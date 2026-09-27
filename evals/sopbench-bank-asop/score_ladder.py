"""Score the SOPBench procedure ladder with SOPBench's own deterministic evaluator.

Produces per-task boolean vectors per arm (so arms can be compared paired, on the same
tasks) plus the aggregate rates SOPBench itself reports. No LLM anywhere.
"""
import json, sys, os
from math import comb
from pathlib import Path

SOPBENCH = Path(os.environ.get("SOPBENCH_DIR", Path.home() / "Code" / "SOPBench"))
sys.path.insert(0, str(SOPBENCH))
from env.evaluator import evaluator_function_directed_graph
from run_evaluation import try_eval  # SOPBench's own arg/content parsing — keep scoring identical

# Raw trajectories are large and live outside the repo; point at them with LADDER_DIR.
LADDER = Path(os.environ.get("LADDER_DIR", Path.cwd() / "ladder"))
FNAME = "ast_openai_gpt-oss-20b-mode_fc-dep_full-fmt_structured-tool_full-shuffle_False.json"
ARMS = ["none", "hint", "order", "pva"]
METRICS = ["success", "dirgraph_satisfied", "constraint_not_violated",
           "database_match", "action_called_correctly", "no_tool_call_error"]


def score_arm(domain, arm):
    path = str(LADDER / domain / arm / domain / FNAME)
    sims = json.load(open(path))
    out = {}
    for idx, sim in enumerate(sims):
        if not sim["interactions"]:
            continue  # all retries exhausted; excluded from every arm by the key intersection
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
                    func_calls.append({
                        "tool_name": interaction[i + 1]["tool_name"],
                        "arguments": try_eval(interaction[i]["tool_calls"][0]["function"]["arguments"]),
                        "content": try_eval(interaction[i + 1]["content"]),
                    })
        ev = evaluator_function_directed_graph(
            domain_str=sim["domain"], task=sim["task"],
            log_msg_fcall=interaction, func_calls=func_calls,
            results=results, default_constraint_option="full")
        key = (sim["task"]["user_goal"], idx)
        out[key] = {m: bool(ev[m]) for m in METRICS}
        out[key]["action_should_succeed"] = bool(sim["task"]["action_should_succeed"])
        out[key]["num_function_calls"] = len(func_calls)
    return out


def mcnemar_exact(b, c):
    """Two-sided exact binomial p for discordant pairs b (A-only wins) vs c (B-only wins)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def main(domain):
    arms = {}
    for a in ARMS:
        p = str(LADDER / domain / a / domain / FNAME)
        if os.path.exists(p):
            arms[a] = score_arm(domain, a)
    keys = set.intersection(*[set(v.keys()) for v in arms.values()])
    keys = sorted(keys, key=lambda k: k[1])
    print(f"\n### {domain} — n={len(keys)} tasks common to all {len(arms)} arms\n")
    hdr = f"{'arm':10s}" + "".join(f"{m[:14]:>16s}" for m in METRICS)
    print(hdr)
    for a, v in arms.items():
        row = f"{a:10s}"
        for m in METRICS:
            row += f"{sum(v[k][m] for k in keys)/len(keys):>16.3f}"
        print(row)

    print(f"\npaired vs baseline 'none' (exact McNemar on `success`, n={len(keys)}):")
    base = arms["none"]
    for a in ARMS[1:]:
        if a not in arms:
            continue
        v = arms[a]
        b = sum(1 for k in keys if v[k]["success"] and not base[k]["success"])
        c = sum(1 for k in keys if base[k]["success"] and not v[k]["success"])
        n = len(keys)
        d = (sum(v[k]["success"] for k in keys) - sum(base[k]["success"] for k in keys)) / n
        # paired difference of proportions: variance from the discordant cells only
        se = ((b + c - (b - c) ** 2 / n) ** 0.5) / n if (b + c) else 0.0
        lo, hi = d - 1.96 * se, d + 1.96 * se
        print(f"  {a:10s} delta={d:+.3f} [{lo:+.3f},{hi:+.3f}]  gained={b:3d} lost={c:3d}  p={mcnemar_exact(b,c):.4f}")

    print("\nsplit by whether the action SHOULD have succeeded (permissible vs impermissible):")
    perm = [k for k in keys if base[k]["action_should_succeed"]]
    imp = [k for k in keys if not base[k]["action_should_succeed"]]
    print(f"  {'arm':10s}{'permissible n=%d'%len(perm):>22s}{'impermissible n=%d'%len(imp):>22s}{'avg calls':>12s}")
    for a, v in arms.items():
        pr = sum(v[k]["success"] for k in perm)/len(perm) if perm else float('nan')
        ir = sum(v[k]["success"] for k in imp)/len(imp) if imp else float('nan')
        ac = sum(v[k]["num_function_calls"] for k in keys)/len(keys)
        print(f"  {a:10s}{pr:>22.3f}{ir:>22.3f}{ac:>12.2f}")

    json.dump({a: {f"{k[0]}#{k[1]}": v[k] for k in keys} for a, v in arms.items()},
              open(str(LADDER / f"{domain}-scored.json"), "w"), indent=1)


if __name__ == "__main__":
    main(sys.argv[1])
