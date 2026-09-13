#!/usr/bin/env python3
"""T1: does a gate verdict track whether the precondition actually held?

    t1_label.py sample --run <dir> --out worksheet.jsonl [--n 60]
    t1_label.py score  --worksheet worksheet.jsonl

WHY THIS EXISTS, AND WHY IT IS NOT A PASS RATE
-----------------------------------------------
An earlier attempt answered "do gates discriminate?" with a run-level
statistic: mean refusals on runs that passed (4.25) versus runs that failed
(4.27), concluding the gate was a constant. Three things were wrong with it.

Gate discrimination is a property of each DECISION, not of a run. A run fails
for reasons no gate touches — bad tool arguments, max turns, a mutation error —
and succeeds despite a wrongly-passed gate when the error never reached the
final hash. A run-level refusal count answers a different question.

The count was also partly an artifact: `max_refusals` means a stuck step
mechanically emits exactly that many refusals before escalation, whatever the
judgment was.

And "no run passed every gate" is what COMPOUNDING produces. A verifier at 0.85
per-decision accuracy across eight sequential gates clears a whole run about
27% of the time. An imperfect-but-real verifier looks exactly like a useless
one at that resolution.

So: the unit is one gate decision, and the label comes from a human reading the
same evidence the verifier saw. A classifier scored against its own output
measures nothing, which rules out using the verdict log as its own truth.

THE STRATIFICATION IS THE POINT
--------------------------------
Cross-review named the way this programme most plausibly fools itself: widen
the verifier's view and the refusal gap improves TAUTOLOGICALLY, because runs
that are going well produce richer, cleaner evidence by construction. The
verifier gets better at detecting "this transcript smells successful", which is
not the same as judging whether a precondition held — and an aggregate
precision/recall averages that away completely.

So every figure here is also reported split by the run's EVENTUAL OUTCOME. If
accuracy on decisions with the same true label differs between runs that went
on to pass and runs that went on to fail, the verifier is reading the room
rather than the evidence. That split is the tautology detector, and it is the
reason this file exists rather than a one-line mean.
"""

from __future__ import annotations

import argparse
import collections
import json
import random
import re
from pathlib import Path

TRUTH_HELP = """\
For each decision below, read `evidence` — the same tool history and step turns
the verifier saw — and set "truth" to:

  "held"     the step's precondition WAS satisfied by that evidence
  "not_held" it was not
  "unclear"  the evidence genuinely does not settle it

Do not look at the verifier's own verdict first; it is kept in the record only
so scoring can compare, and reading it first is how a labelling exercise
becomes an agreement exercise. "unclear" is a real answer and is excluded from
precision and recall rather than being forced either way.
"""


def load_run(run: Path) -> list[dict]:
    """Verdicts joined to the outcome of the run they came from."""
    verdicts = [
        json.loads(line)
        for line in (run / "verdicts.jsonl").read_text().splitlines()
        if line.strip()
    ]
    sims = json.loads((run / "results.json").read_text())["simulations"]
    # Conversations are numbered in creation order at concurrency 1, which is
    # what run_arm_c enforces; anything else would mis-join and the join is
    # asserted rather than assumed.
    convs = sorted({v["conversation"] for v in verdicts})
    if len(convs) != len(sims):
        raise SystemExit(
            f"{len(convs)} conversations but {len(sims)} simulations — cannot "
            "join verdicts to outcomes. Was the run concurrent?"
        )
    outcome = {
        c: {
            "task": s["task_id"],
            "run_passed": s["reward_info"]["reward"] == 1.0,
        }
        for c, s in zip(convs, sims)
    }
    for v in verdicts:
        v.update(outcome[v["conversation"]])
    return verdicts


def sample(args) -> int:
    all_rows = load_run(args.run)
    # Escalation records are written by the escalation path rather than by a
    # verdict, so they carry no evidence. A decision a labeller cannot read is
    # a decision they would be guessing at, so they are dropped here and the
    # count is printed rather than quietly shrinking the sample.
    rows = [r for r in all_rows if "evidence" in r]
    dropped = len(all_rows) - len(rows)
    if not rows:
        raise SystemExit(
            "no evidence recorded — re-run with ASOP_RECORD_EVIDENCE=1, or a "
            "labeller has nothing to read and would be guessing."
        )
    if dropped:
        print(f"[t1] {dropped} escalation record(s) excluded: no evidence attached")

    # Stratify so no cell can be silently absent. The outcome axis is the one
    # that matters; the others stop a sample being all of one step.
    buckets: dict[tuple, list[dict]] = collections.defaultdict(list)
    for r in rows:
        buckets[(r["run_passed"], r["gate"], r["passed"])].append(r)

    rng = random.Random(args.seed)
    per = max(1, args.n // max(1, len(buckets)))
    out: list[dict] = []
    for key, items in sorted(buckets.items(), key=lambda kv: str(kv[0])):
        rng.shuffle(items)
        out.extend(items[:per])

    for i, r in enumerate(out):
        r["truth"] = ""  # to be filled by a person
        r["_id"] = i

    args.out.write_text(
        json.dumps({"_help": TRUTH_HELP, "_cells": len(buckets)}) + "\n"
        + "\n".join(json.dumps(r) for r in out)
        + "\n"
    )
    print(f"[t1] {len(rows)} labellable decisions -> {len(out)} sampled across {len(buckets)} cells")
    print(f"[t1] worksheet -> {args.out}")
    print("[t1] fill in every \"truth\" field, then run: t1_label.py score")
    return 0


# ── model-free ground truth ──────────────────────────────────────────────────
#
# Some preconditions have an objective reading that a deterministic rule can
# settle from the tool history: "the user must provide their user id" is
# satisfied exactly when a get_user_details call returned ok. No model, no
# verifier, no human judgment — which is what makes it usable as a LABEL for
# the verifier rather than another opinion to compare against.
#
# Two honesty notes that belong next to the code, not in a footnote:
#
#   * These are PROXIES. "a successful lookup happened" stands in for "the user
#     was identified". Defensible, and not the same sentence. Where the proxy
#     and the precondition could come apart, the decision is left for a human.
#   * Using the run's own evidence to build the label is not the leakage the
#     adapter guards against. The gate never sees this; it is computed offline,
#     afterwards, and never fed back. Scoring a classifier against labels is the
#     point of having labels. Deriving them from the GOLD ACTIONS would be
#     leakage, and that is why no predicate here touches them.


def _last_result(tool_history: tuple, tool: str) -> str:
    """ok / failed / absent for the most recent call to `tool`."""
    state = "absent"
    for i, line in enumerate(tool_history):
        if line.startswith(f"called {tool}("):
            nxt = tool_history[i + 1] if i + 1 < len(tool_history) else ""
            state = "ok" if nxt.startswith("  -> ok") else "failed"
    return state


def _user_identified(ev: dict) -> str:
    return {"ok": "held", "failed": "not_held", "absent": "not_held"}[
        _last_result(tuple(ev["tool_history"]), "get_user_details")
    ]


def _reservation_located(ev: dict) -> str:
    return {"ok": "held", "failed": "not_held", "absent": "not_held"}[
        _last_result(tuple(ev["tool_history"]), "get_reservation_details")
    ]


PREDICATES = [
    # (what the step's precondition must mention, name, rule)
    (re.compile(r"user\s*id", re.I), "user_identified", _user_identified),
    (re.compile(r"reservation\s*id", re.I), "reservation_located", _reservation_located),
]


def derive_truth(row: dict) -> tuple[str, str]:
    """(label, which rule), or ("", "") when no rule applies.

    A step naming several checkable preconditions must satisfy ALL of them —
    "obtain user id AND reservation id" is not half-held.
    """
    body = row["evidence"]["step_body"]
    applied, verdicts = [], []
    for pattern, name, rule in PREDICATES:
        if pattern.search(body):
            applied.append(name)
            verdicts.append(rule(row["evidence"]))
    if not applied:
        return "", ""
    label = "held" if all(v == "held" for v in verdicts) else "not_held"
    return label, "+".join(applied)


def derive(args) -> int:
    lines = [l for l in args.worksheet.read_text().splitlines() if l.strip()]
    header, rows = lines[0], [json.loads(l) for l in lines[1:]]

    auto = 0
    for r in rows:
        if r.get("truth"):
            continue
        label, rule = derive_truth(r)
        if label:
            r["truth"] = label
            r["truth_source"] = f"rule:{rule}"
            auto += 1

    args.worksheet.write_text(
        header + "\n" + "\n".join(json.dumps(r) for r in rows) + "\n"
    )
    left = sum(1 for r in rows if not r.get("truth"))
    print(f"[t1] {auto} of {len(rows)} labelled by deterministic rule")
    print(f"[t1] {left} still need a person — no rule has an objective reading")
    if auto:
        print("[t1] these are PROXY labels; the rule name is recorded on each row")
    return 0


def _pr(rows: list[dict]) -> dict:
    """Precision and recall of REFUSAL as a detector of an unmet precondition."""
    tp = sum(1 for r in rows if not r["passed"] and r["truth"] == "not_held")
    fp = sum(1 for r in rows if not r["passed"] and r["truth"] == "held")
    fn = sum(1 for r in rows if r["passed"] and r["truth"] == "not_held")
    tn = sum(1 for r in rows if r["passed"] and r["truth"] == "held")
    n = tp + fp + fn + tn
    return {
        "n": n,
        "precision": tp / (tp + fp) if (tp + fp) else None,
        "recall": tp / (tp + fn) if (tp + fn) else None,
        "accuracy": (tp + tn) / n if n else None,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
    }


def _fmt(label: str, m: dict) -> str:
    def pct(x):
        return f"{x:.2f}" if isinstance(x, float) else "  — "
    return (
        f"  {label:<26}n={m['n']:<4} precision={pct(m['precision'])} "
        f"recall={pct(m['recall'])} accuracy={pct(m['accuracy'])}"
    )


def score(args) -> int:
    lines = [l for l in args.worksheet.read_text().splitlines() if l.strip()]
    rows = [json.loads(l) for l in lines[1:]]
    labelled = [r for r in rows if r.get("truth") in ("held", "not_held")]
    unclear = sum(1 for r in rows if r.get("truth") == "unclear")
    missing = len(rows) - len(labelled) - unclear
    if missing:
        print(f"[t1] {missing} of {len(rows)} decisions are still unlabelled")
    if not labelled:
        raise SystemExit("nothing labelled yet")

    print(f"\n[t1] {len(labelled)} labelled, {unclear} unclear (excluded)\n")
    print("OVERALL — refusal as a detector of an unmet precondition")
    print(_fmt("all decisions", _pr(labelled)))

    print("\nTHE TAUTOLOGY CHECK — same question, split by how the run ended")
    passed = [r for r in labelled if r["run_passed"]]
    failed = [r for r in labelled if not r["run_passed"]]
    mp, mf = _pr(passed), _pr(failed)
    print(_fmt("runs that passed", mp))
    print(_fmt("runs that failed", mf))
    if mp["accuracy"] is not None and mf["accuracy"] is not None:
        gap = abs(mp["accuracy"] - mf["accuracy"])
        print(f"\n  accuracy gap between the two: {gap:.2f}")
        if gap > 0.15:
            print(
                "  A large gap means the verifier is reading the RUN, not the\n"
                "  precondition — the same decision judged differently depending\n"
                "  on how things turned out. That is the tautology, and an\n"
                "  aggregate precision/recall would have hidden it."
            )
        else:
            print("  Small gap: no evidence the verifier is reading the room.")

    print("\nBY DECLARED GATE KIND — a label from the ASOP text, not how it was computed")
    by_kind = collections.defaultdict(list)
    for r in labelled:
        by_kind[r.get("declared_gate", r["gate"])].append(r)
    for kind, rs in sorted(by_kind.items()):
        print(_fmt(kind, _pr(rs)))

    print(
        "\nNote: only `deterministic` verdicts naming a tool were computed without\n"
        "a model. Every other kind in this log went through the same judge call,\n"
        "whatever its label says."
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s1 = sub.add_parser("sample")
    s1.add_argument("--run", type=Path, required=True)
    s1.add_argument("--out", type=Path, required=True)
    s1.add_argument("--n", type=int, default=60)
    s1.add_argument("--seed", type=int, default=20260912)
    s1.set_defaults(fn=sample)

    s3 = sub.add_parser("derive")
    s3.add_argument("--worksheet", type=Path, required=True)
    s3.set_defaults(fn=derive)

    s2 = sub.add_parser("score")
    s2.add_argument("--worksheet", type=Path, required=True)
    s2.set_defaults(fn=score)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
