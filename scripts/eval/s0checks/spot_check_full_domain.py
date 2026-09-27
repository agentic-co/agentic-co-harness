#!/usr/bin/env python3
"""Library twin of spot_check_dump.py: sample N (seeded) constraint-leaf rows
across every task in a domain (no prior judge log to sample from -- see
score_full_domain.py), dumping tool evidence + resolved params + verdict +
truth for each, for human spot-checking.

Usage:
    python3 spot_check_full_domain.py --domain library \
        --evidence evals/s0checks/library/evidence.json \
        --checks evals/s0checks/library/checks --n 20 --seed 20260926 \
        --out evals/s0checks/library/spot_check_20.json
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score_full_domain import resolve_params, load_check_fns  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sopbench", type=Path, default=Path.home() / "Code" / "SOPBench")
    ap.add_argument("--domain", required=True)
    ap.add_argument("--evidence", type=Path, required=True)
    ap.add_argument("--checks", type=Path, required=True)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20260926)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    import json
    evidence = json.loads(args.evidence.read_text())
    check_fns = load_check_fns(args.checks)

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(args.sopbench))
    from grade_judge_gate import load_tasks, truth_of
    from sopbench_judge_gate import task_leaves

    cwd = os.getcwd()
    os.chdir(args.sopbench)
    tasks = load_tasks(args.sopbench, args.domain)
    os.chdir(cwd)

    rows = []  # (task_key, task, name, leaf)
    for tk, (_pos, task) in tasks.items():
        os.chdir(args.sopbench)
        leaves = task_leaves(task.get("constraints"))
        os.chdir(cwd)
        for name, leaf in leaves.items():
            rows.append((tk, task, name, leaf))

    rng = random.Random(args.seed)
    sample = rng.sample(rows, min(args.n, len(rows)))

    out = []
    for tk, task, name, leaf in sample:
        polarity = not name.startswith("not ")
        constraint = name[4:] if not polarity else name
        os.chdir(args.sopbench)
        t = truth_of(args.domain, task, name, leaf[2] if leaf else {})
        os.chdir(cwd)
        ev = evidence.get(tk, {})
        resolved_params = resolve_params(leaf[2] if leaf else None, task)
        fn = check_fns.get(constraint)
        try:
            raw = fn(ev.get("tool_results", {}), resolved_params) if fn else None
        except Exception as exc:
            raw = f"EXCEPTION {type(exc).__name__}: {exc}"
        predicted = raw if isinstance(raw, bool) and polarity else (
            (not raw) if isinstance(raw, bool) else None)
        out.append({
            "task_key": tk, "goal": task.get("user_goal"), "constraint": constraint,
            "polarity": polarity, "tool_results_seen": ev.get("tool_results", {}),
            "resolved_params_seen": resolved_params, "check_raw_output": raw,
            "check_predicted_stated_condition": predicted, "sopbench_truth_of": t,
            "match": (predicted == t) if isinstance(t, bool) and predicted is not None else None,
        })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2, default=str))
    n_match = sum(1 for e in out if e.get("match") is True)
    n_mismatch = sum(1 for e in out if e.get("match") is False)
    print(f"wrote {len(out)} rows to {args.out}: {n_match} match, {n_mismatch} mismatch, "
          f"{len(out) - n_match - n_mismatch} abstain/ungradable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
