"""Score the ASOP port arms against T2's procedure ladder, paired, on identical tasks.

Six arms, one scoring function, SOPBench's own deterministic evaluator, no LLM
anywhere in the loop:

    none | constraint_hint | action_order | pva   <- T2-PROCEDURE-LADDER.md
    asop-prompt | asop-gated                      <- this port

⚠️ **The self-check that makes this trustworthy is `--verify-t2`.** T2 published
`none` 0.500 and `pva` 0.642. This script re-scores T2's own raw trajectories
with the function below and asserts it reproduces those numbers exactly. If it
does, the two new arms are scored by provably the same code as the four they
are being compared against, and any delta is the arms rather than the scorer.
If it does not, nothing here is comparable to T2 and the run says so instead of
quoting a number.

Scoring is deliberately a copy of `score_ladder.py`'s body rather than an import
of it: one function scores all six arms, so the new arms cannot drift from the
old ones through a path only one of them takes.
"""

from __future__ import annotations

import json
import os
import sys
from math import comb
from pathlib import Path

# Both locations are per-machine and per-session, so they are read from the
# environment rather than written down: a checked-in absolute path leaks an
# account name into a public repo and pins the script to one machine.
#   SOPBENCH_HOME  the SOPBench checkout          (default ~/Code/SOPBench)
#   ASOP_SCRATCH   where the raw trajectories are (no default — it is a
#                  session scratchpad, and guessing one would silently score
#                  the wrong run or none at all)
SOPBENCH_HOME = Path(os.environ.get("SOPBENCH_HOME", Path.home() / "Code" / "SOPBench"))
sys.path.insert(0, str(SOPBENCH_HOME))
from env.evaluator import evaluator_function_directed_graph  # noqa: E402
from run_evaluation import try_eval  # noqa: E402  SOPBench's own parsing — keep scoring identical

SP = os.environ.get("ASOP_SCRATCH", "")
# The executor is encoded in SOPBench's output filename. `ASOP_MODEL` names a
# different one (e.g. `glm-4.7` for the gate test) without touching the default,
# so every published gpt-oss-20b score reproduces unchanged; `ASOP_FNAME`
# overrides the whole name if a run used non-default SOPBench flags.
_MODEL = os.environ.get("ASOP_MODEL", "openai/gpt-oss-20b")
FNAME = os.environ.get(
    "ASOP_FNAME",
    f"ast_{_MODEL.replace('/', '_')}-mode_fc-dep_full-fmt_structured-tool_full-shuffle_False.json",
)

# ⚠️ HELD-OUT DOMAINS (ASOP-V2-ITERATION.md §4). Rounds are iterated on `bank`
# and measured on `bank`, which by itself produces a number that means nothing.
# `ASOP_DOMAIN` points this same scorer — not a second one — at a domain the
# iteration never saw. Same function, same SOPBench evaluator, same self-check
# posture, so a held-out delta is comparable to a development one.
DOMAIN = os.environ.get("ASOP_DOMAIN", "bank")

METRICS = [
    "success",
    "dirgraph_satisfied",
    "constraint_not_violated",
    "database_match",
    "action_called_correctly",
    "no_tool_call_error",
]

# T2's four arms, plus this port's two. Paths differ because the ladder arms
# were run by T2's sweep and these by `run_sopbench_asop.py`.
ARM_PATHS = {
    "none": f"{SP}/ladder/{DOMAIN}/none/{DOMAIN}/{FNAME}",
    "constraint_hint": f"{SP}/ladder/{DOMAIN}/hint/{DOMAIN}/{FNAME}",
    "action_order": f"{SP}/ladder/{DOMAIN}/order/{DOMAIN}/{FNAME}",
    "pva": f"{SP}/ladder/{DOMAIN}/pva/{DOMAIN}/{FNAME}",
    "asop-prompt": f"{SP}/final/prompt/{DOMAIN}/{FNAME}",
    "asop-gated": f"{SP}/final/gated/{DOMAIN}/{FNAME}",
    # Same document, same runtime, same config — the ONLY difference is that
    # the deterministic gate resolves on the value a verification tool returned
    # rather than on the tool having run. See `run_sopbench_asop.--value-gate`.
    "asop-gated-value": f"{SP}/vg/gated/{DOMAIN}/{FNAME}",
    # ── N28: the two variables that were confounded inside "our gate loses" ──
    #
    # Every arm above `asop-gated` keeps SOPBench's own task instructions — the
    # constraint specification and its thresholds — and appends to them. The
    # gated arms REPLACE them (`agent.instructions = prompt`), so they were the
    # only arms running without the numbers their own steps told them to check
    # against. These two restore that block (`--host-rules`) and then differ in
    # one thing only:
    #
    #   -rules    stepwise presentation, unchanged: one step's body at a time.
    #   -upfront  the whole per-task checklist at once, enumerate-then-act.
    #
    # So `-rules` minus `asop-gated-value` isolates the INFORMATION effect, and
    # `-upfront` minus `-rules` isolates the PRESENTATION effect that N27
    # hypothesised and could not separate. Same document, same value gate, same
    # executor, same config throughout.
    "asop-gated-value-rules": f"{SP}/hr/gated/{DOMAIN}/{FNAME}",
    "asop-gated-value-upfront": f"{SP}/uf/gated/{DOMAIN}/{FNAME}",
    # ── ASOP V2 (ASOP-V2-ITERATION.md) ──────────────────────────────────────
    #
    # Every V2 arm keeps `--value-gate --host-rules` and stepwise presentation,
    # so `asop-gated-value-rules` above is the arm each of them differs from by
    # exactly one thing. Round numbers are development numbers measured on
    # `bank`; only the held-out domains at the end are the result.
    #
    #   v2-doc      the V2 compiled document, gate unchanged. Isolates the
    #               AUTHORING change — topological step order (the defect that
    #               made 38 of 44 dirgraph failures), ESTABLISH/VERIFY,
    #               OR groups, one stated discipline asking for a verdict.
    #   v2-verdict  + `--verdict-gate`. Isolates the ARCHITECTURE change: the
    #               executor attests, the gate audits the attestation.
    # v1's document with ONLY the step order corrected — every step's text,
    # gate and tool are v1's, and the runtime configuration is
    # `asop-gated-value-rules`'s. It is the single cleanest question available:
    # were five configurations of gate-architecture work downstream of a
    # compiler bug? (§0 of ASOP-V2-ITERATION.md.)
    "asop-v1-order": f"{SP}/r0b/gated/{DOMAIN}/{FNAME}",
    "asop-v2-doc": f"{SP}/r1/gated/{DOMAIN}/{FNAME}",
    "asop-v2-verdict": f"{SP}/r2/gated/{DOMAIN}/{FNAME}",
    "asop-v2-r3": f"{SP}/r3/gated/{DOMAIN}/{FNAME}",
    "asop-v2-r4": f"{SP}/r4/gated/{DOMAIN}/{FNAME}",
    "asop-v2-r5": f"{SP}/r5/gated/{DOMAIN}/{FNAME}",
    # The FINAL configuration, run cold on a domain it has never seen. This is
    # the only row on the page that counts as a result.
    "asop-v2-heldout": f"{SP}/heldout/{DOMAIN}/gated/{DOMAIN}/{FNAME}",
    # EXP-A (QUEUED-EXPERIMENTS.md, N31): `asop-v2-doc` + `--restore-helper-prereqs`,
    # nothing else. Run as 2 shards on 2 model copies and merged
    # (`sopbench_shard.py`); verified 6/6 identical to an unsharded run first.
    "asop-v2-expa": f"{SP}/expa/gated/{DOMAIN}/{FNAME}",
    # ── The gate test (QUEUED-EXPERIMENTS.md "Gate test (Stage 1)"): z.ai GLM-4.7
    # executor on `hotel`, run with ASOP_MODEL=glm-4.7 ASOP_DOMAIN=hotel. The
    # baselines live under `ladder/` like T2's, so `none`/`pva` above resolve to
    # the GLM runs automatically when those env vars are set.
    "asop-v2-toolgate": f"{SP}/gate/toolgate/{DOMAIN}/{FNAME}",
    "asop-v2-jev": f"{SP}/gate/jev/{DOMAIN}/{FNAME}",
    "asop-v2-stringall": f"{SP}/gate/stringall/{DOMAIN}/{FNAME}",
    "asop-v2-laya": f"{SP}/gate/laya/{DOMAIN}/{FNAME}",
    # A7 (idea 4): `asop-v2-jev`'s exact config + `--judge-gate-mode advisory`
    # — a not_held Jev verdict below the hard-block confidence PASSES with a
    # note queued for the executor's next prompt instead of hard-stopping.
    # Run sharded (--shard 0/2, 1/2 against z.ai, merged with sopbench_shard.py
    # merge) — see ai-tasks/local-s1s2/PLAN.md for the pre-registered design.
    "asop-v2-jev-advisory": f"{SP}/gate/advisory/{DOMAIN}/{FNAME}",
}

# ⚠️ Goals with NO compiled procedure, because they are absent from bank's
# `actions` schema and `create_assistant` therefore never exposes them as tools.
# 22 of 134 tasks (14 should-fail, 8 should-succeed).
#
# This is NOT symmetric across arms, which is why it gets its own table rather
# than a footnote. No arm can perform these actions. But the GATED arm cannot
# even route to them — there is no procedure — so it stays in triage, makes zero
# substantive tool calls and exits. Measured on the first 17 such tasks: 1.0
# calls per task (the `exit_conversation` itself) against the prompt arm's 2.1.
#
# An arm that calls nothing cannot violate a constraint, so it collects
# `constraint_not_violated` and very likely `success` on all 14 should-fail
# tasks for free — roughly +10 points of pass rate, concentrated entirely in the
# impermissible half, bought by being unable to act rather than by gating well.
# That is exactly the "over-gating buys the refusal column by selling the
# completion column" artifact T2's split exists to expose, arriving through a
# schema gap instead of through the gate.
#
# So the headline comparison for this port is the ROUTABLE subset. The full set
# is reported too, because hiding it would be its own distortion.
NO_PROCEDURE_GOALS_BY_DOMAIN = {
    "bank": frozenset({"cancel_credit_card", "pay_bill_with_credit_card"}),
}
# A domain whose goals are all routable drops nothing, and the routable subset
# is then the full set — which the printout states rather than implying.
NO_PROCEDURE_GOALS = NO_PROCEDURE_GOALS_BY_DOMAIN.get(DOMAIN, frozenset())

# T2-PROCEDURE-LADDER.md's published `success` rates, used as the scorer check.
T2_PUBLISHED_BY_DOMAIN = {
    "bank": {"none": 0.500, "constraint_hint": 0.552, "action_order": 0.634, "pva": 0.642},
    "online_market": {"none": 0.384, "constraint_hint": 0.616, "action_order": 0.541, "pva": 0.709},
    "library": {"none": 0.227, "constraint_hint": 0.379, "action_order": 0.364, "pva": 0.561},
}
# `ASOP_SUBSET` names a JSON list of task POSITIONS (e.g. the hotel pilot); every
# arm is scored on those positions only. Default: all tasks, unchanged.
_SUBSET_FILE = os.environ.get("ASOP_SUBSET")
SUBSET = frozenset(json.load(open(_SUBSET_FILE))) if _SUBSET_FILE else None
# The self-check reproduces T2's published gpt-oss-20b rates on ALL tasks. On a
# subset, another executor, or a domain T2 never published it has nothing to
# reproduce — so it is skipped and says why, rather than printing a false DRIFT.
_SELF_CHECK_SKIP = (
    "subset run" if SUBSET is not None
    else f"executor {_MODEL} is not T2's" if _MODEL != "openai/gpt-oss-20b"
    else f"T2 published no rates for {DOMAIN}" if DOMAIN not in T2_PUBLISHED_BY_DOMAIN
    else None
)
T2_PUBLISHED = {} if _SELF_CHECK_SKIP else T2_PUBLISHED_BY_DOMAIN[DOMAIN]


def score_file(path: str) -> dict:
    """One arm's per-task boolean vectors, via SOPBench's own evaluator."""
    sims = json.load(open(path))
    out: dict = {}
    for idx, sim in enumerate(sims):
        if SUBSET is not None and idx not in SUBSET:
            continue
        if not sim["interactions"]:
            # All retries exhausted. Excluded from EVERY arm by the key
            # intersection below, so one arm's faulted cell cannot silently
            # become another arm's advantage.
            continue
        interaction_log = sim["interactions"][0]
        results = {"final_database": interaction_log["database"]}
        interaction = interaction_log["interaction"]
        func_calls = []
        for i in range(len(interaction) - 1):
            if interaction[i].get("tool_calls", []):
                for tc in list(interaction[i]["tool_calls"]):
                    if tc["function"]["name"].lower() in ["n/a", "na", "none", "null"]:
                        interaction[i]["tool_calls"].remove(tc)
                if len(interaction[i]["tool_calls"]) > 0:
                    func_calls.append(
                        {
                            "tool_name": interaction[i + 1]["tool_name"],
                            "arguments": try_eval(
                                interaction[i]["tool_calls"][0]["function"]["arguments"]
                            ),
                            "content": try_eval(interaction[i + 1]["content"]),
                        }
                    )
        ev = evaluator_function_directed_graph(
            domain_str=sim["domain"],
            task=sim["task"],
            log_msg_fcall=interaction,
            func_calls=func_calls,
            results=results,
            default_constraint_option="full",
        )
        key = (sim["task"]["user_goal"], idx)
        out[key] = {m: bool(ev[m]) for m in METRICS}
        out[key]["action_should_succeed"] = bool(sim["task"]["action_should_succeed"])
        out[key]["num_function_calls"] = len(func_calls)
    return out


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact binomial p for discordant pairs b (A-only) vs c (B-only)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(comb(n, i) for i in range(0, k + 1)) / (2**n)
    return min(1.0, 2 * tail)


def paired(arms: dict, keys: list, a: str, base: str = "none") -> dict:
    v, b0 = arms[a], arms[base]
    b = sum(1 for k in keys if v[k]["success"] and not b0[k]["success"])
    c = sum(1 for k in keys if b0[k]["success"] and not v[k]["success"])
    n = len(keys)
    d = (sum(v[k]["success"] for k in keys) - sum(b0[k]["success"] for k in keys)) / n
    se = ((b + c - (b - c) ** 2 / n) ** 0.5) / n if (b + c) else 0.0
    return {
        "delta": d,
        "lo": d - 1.96 * se,
        "hi": d + 1.96 * se,
        "gained": b,
        "lost": c,
        "p": mcnemar_exact(b, c),
    }


def main() -> int:
    if not SP:
        print(
            "ASOP_SCRATCH is unset. Point it at the directory holding the raw\n"
            "trajectories (it must contain `ladder/bank/...` for T2's arms and\n"
            "`run/{gated,prompt}/bank/...` for this port's).",
            file=sys.stderr,
        )
        return 2
    arms: dict = {}
    for name, path in ARM_PATHS.items():
        if os.path.exists(path):
            arms[name] = score_file(path)
        else:
            print(f"[skip] {name}: no trajectories at {path}", file=sys.stderr)

    if "none" not in arms:
        print("baseline `none` is missing — nothing can be paired", file=sys.stderr)
        return 1

    keys = sorted(set.intersection(*[set(v) for v in arms.values()]), key=lambda k: k[1])
    print(f"\n### {DOMAIN} — n={len(keys)} tasks common to all {len(arms)} arms: {', '.join(arms)}\n")

    # -- the scorer self-check ------------------------------------------
    # Computed on the intersection of T2's OWN four arms, never on the six-arm
    # intersection: a port arm still running has fewer tasks on disk, which
    # would shrink the common set and make T2's rates "drift" for a reason that
    # has nothing to do with the scorer. That exact false alarm fired the first
    # time this ran, at n=1.
    t2_present = [a for a in T2_PUBLISHED if a in arms]
    t2_keys = (
        sorted(set.intersection(*[set(arms[a]) for a in t2_present]), key=lambda k: k[1])
        if t2_present
        else []
    )
    if _SELF_CHECK_SKIP:
        print(f"scorer check against T2's published rates: SKIPPED ({_SELF_CHECK_SKIP})")
    else:
        print(
            f"scorer check against T2-PROCEDURE-LADDER.md's published rates "
            f"(on T2's own n={len(t2_keys)} intersection):"
        )
    drift = []
    for a, want in T2_PUBLISHED.items():
        if a not in arms:
            continue
        got = sum(arms[a][k]["success"] for k in t2_keys) / len(t2_keys)
        mark = "ok" if abs(got - want) < 0.0005 else "DRIFT"
        if mark == "DRIFT":
            drift.append((a, want, got))
        print(f"  {a:16s} published={want:.3f}  recomputed={got:.3f}  {mark}")
    if drift:
        print(
            "\n*** SCORER DRIFT: these arms do not reproduce T2's published numbers,\n"
            "    so the new arms are NOT comparable to them. Do not quote a delta.\n",
            file=sys.stderr,
        )
    print()

    hdr = f"{'arm':16s}" + "".join(f"{m[:14]:>16s}" for m in METRICS)
    print(hdr)
    for a, v in arms.items():
        row = f"{a:16s}"
        for m in METRICS:
            row += f"{sum(v[k][m] for k in keys) / len(keys):>16.3f}"
        print(row)

    print(f"\npaired vs baseline 'none' (exact McNemar on `success`, n={len(keys)}):")
    for a in arms:
        if a == "none":
            continue
        r = paired(arms, keys, a)
        print(
            f"  {a:16s} delta={r['delta']:+.3f} [{r['lo']:+.3f},{r['hi']:+.3f}]  "
            f"gained={r['gained']:3d} lost={r['lost']:3d}  p={r['p']:.4f}"
        )

    # The contrast the whole experiment turns on: document-as-context vs
    # document-plus-gate-enforcement, paired against each other rather than
    # against baseline.
    if "asop-prompt" in arms and "asop-gated" in arms:
        r = paired(arms, keys, "asop-gated", base="asop-prompt")
        print(
            f"\n  asop-gated vs asop-prompt: delta={r['delta']:+.3f} "
            f"[{r['lo']:+.3f},{r['hi']:+.3f}] gained={r['gained']} lost={r['lost']} p={r['p']:.4f}"
        )

    # -- the artifact-free comparison ------------------------------------
    routable = [k for k in keys if k[0] not in NO_PROCEDURE_GOALS]
    dropped = len(keys) - len(routable)
    if dropped and routable:
        print(
            f"\n### ROUTABLE SUBSET — n={len(routable)} ({dropped} tasks dropped: goals with no\n"
            "    compiled procedure, where the gated arm cannot act at all and collects\n"
            "    correct-refusals for free). THIS is the comparison to read.\n"
        )
        # `dirgraph` is in this table and not only the one above because it is
        # the metric that has refused to move across five gated configurations,
        # and the routable subset is the comparison the port is about. Reading
        # it off the full set silently mixes in the 22 tasks no arm can route.
        row = (
            f"{'arm':26s}{'success':>10s}{'permissible':>13s}{'impermissible':>15s}"
            f"{'calls':>8s}{'dirgraph':>10s}{'constr_ok':>11s}"
        )
        print(row)
        rperm = [k for k in routable if arms["none"][k]["action_should_succeed"]]
        rimp = [k for k in routable if not arms["none"][k]["action_should_succeed"]]
        for a, v in arms.items():
            s = sum(v[k]["success"] for k in routable) / len(routable)
            pr = sum(v[k]["success"] for k in rperm) / len(rperm) if rperm else float("nan")
            ir = sum(v[k]["success"] for k in rimp) / len(rimp) if rimp else float("nan")
            ac = sum(v[k]["num_function_calls"] for k in routable) / len(routable)
            dg = sum(v[k]["dirgraph_satisfied"] for k in routable) / len(routable)
            cv = sum(v[k]["constraint_not_violated"] for k in routable) / len(routable)
            print(
                f"{a:26s}{s:>10.3f}{pr:>13.3f}{ir:>15.3f}{ac:>8.2f}{dg:>10.3f}{cv:>11.3f}"
            )
        print(f"\n  paired vs 'none' on the routable subset (n={len(routable)}):")
        for a in arms:
            if a == "none":
                continue
            r = paired(arms, routable, a)
            print(
                f"    {a:16s} delta={r['delta']:+.3f} [{r['lo']:+.3f},{r['hi']:+.3f}]  "
                f"gained={r['gained']:3d} lost={r['lost']:3d}  p={r['p']:.4f}"
            )
        for a, base in (
            ("asop-gated", "asop-prompt"),
            ("asop-gated-value", "asop-prompt"),
            # The clean A/B on the fix: identical document, identical runtime,
            # only the gate's resolution rule differs.
            ("asop-gated-value", "asop-gated"),
            # ── N28's decomposition, and the order matters ──
            # Each line moves EXACTLY one variable from the line above it, so
            # the three deltas add up to the gap rather than overlapping.
            #   information: did the executor have the thresholds at all
            ("asop-gated-value-rules", "asop-gated-value"),
            #   presentation: whole checklist upfront vs one step at a time
            ("asop-gated-value-upfront", "asop-gated-value-rules"),
            #   both together, against the ungated document and against PVA —
            #   the two numbers that say whether any of it closes the gap
            ("asop-gated-value-upfront", "asop-prompt"),
            ("asop-gated-value-upfront", "pva"),
            # ── V2, each line moving exactly one thing from the line above ──
            #   authoring: the V2 document, same gate, same presentation
            #   order alone: v1's text, topologically corrected
            ("asop-v1-order", "asop-gated-value-rules"),
            #   the rest of the rewrite, on top of the order fix
            ("asop-v2-doc", "asop-v1-order"),
            ("asop-v2-doc", "asop-gated-value-rules"),
            #   architecture: + the executor attests and the gate audits it
            ("asop-v2-verdict", "asop-v2-doc"),
            ("asop-v2-r3", "asop-v2-verdict"),
            ("asop-v2-r4", "asop-v2-r3"),
            ("asop-v2-r5", "asop-v2-r4"),
            #   and the two numbers that say whether any of it closed the gap
            ("asop-v2-doc", "none"),
            ("asop-v2-verdict", "pva"),
            #   EXP-A: the ceiling fix alone, then against the scaffold to beat
            ("asop-v2-expa", "asop-v2-doc"),
            ("asop-v2-expa", "pva"),
            #   the gate test: enforcement, each judge against the tool-ran gate
            ("asop-v2-jev", "asop-v2-toolgate"),
            ("asop-v2-stringall", "asop-v2-toolgate"),
            ("asop-v2-jev", "asop-v2-stringall"),
            ("asop-v2-laya", "asop-v2-toolgate"),
            ("asop-v2-laya", "asop-v2-jev"),
            ("asop-v2-toolgate", "pva"),
            ("asop-v2-jev", "pva"),
            #   idea 4 (advisory gate): A7's primary and secondary comparisons —
            #   does letting a not_held verdict pass with a note (instead of
            #   hard-stopping) cost back what blocking bought, against A4
            #   (blocking) and against A3 (tool-gate only, no value gate)?
            ("asop-v2-jev-advisory", "asop-v2-jev"),
            ("asop-v2-jev-advisory", "asop-v2-toolgate"),
        ):
            if a in arms and base in arms:
                r = paired(arms, routable, a, base=base)
                label = f"{a.replace('asop-', '')} vs {base.replace('asop-', '')}"
                print(
                    f"    {label:24s} delta={r['delta']:+.3f} "
                    f"[{r['lo']:+.3f},{r['hi']:+.3f}] gained={r['gained']} "
                    f"lost={r['lost']} p={r['p']:.4f}"
                )

    print("\nsplit by whether the action SHOULD have succeeded (ALL tasks):")
    perm = [k for k in keys if arms["none"][k]["action_should_succeed"]]
    imp = [k for k in keys if not arms["none"][k]["action_should_succeed"]]
    print(
        f"  {'arm':16s}{'permissible n=%d' % len(perm):>22s}"
        f"{'impermissible n=%d' % len(imp):>22s}{'avg calls':>12s}"
    )
    for a, v in arms.items():
        pr = sum(v[k]["success"] for k in perm) / len(perm) if perm else float("nan")
        ir = sum(v[k]["success"] for k in imp) / len(imp) if imp else float("nan")
        ac = sum(v[k]["num_function_calls"] for k in keys) / len(keys)
        print(f"  {a:16s}{pr:>22.3f}{ir:>22.3f}{ac:>12.2f}")

    out = Path(SP) / f"asop-port-{DOMAIN}-scored.json"
    json.dump(
        {a: {f"{k[0]}#{k[1]}": v[k] for k in keys} for a, v in arms.items()},
        open(out, "w"),
        indent=1,
    )
    print(f"\nper-task vectors -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
