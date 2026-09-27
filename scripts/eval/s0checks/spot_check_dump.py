#!/usr/bin/env python3
"""Dump N randomly-sampled (seeded) scored rows for human spot-checking.

For each sampled row: the constraint+polarity, the EXACT tool_results dict the
check function saw, the resolved params it saw, the check's verdict, and
SOPBench's own ground truth -- so a reviewer can independently recompute the
check by hand and compare.

Usage:
    python3 spot_check_dump.py --domain hotel \
        --log <jev.verdicts.jsonl> --evidence evals/s0checks/hotel/evidence.json \
        --checks evals/s0checks/hotel/checks --n 20 --seed 20260926 \
        --out evals/s0checks/hotel/spot_check_20.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score_hotel import resolve_params, load_check_fns  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sopbench", type=Path, default=Path.home() / "Code" / "SOPBench")
    ap.add_argument("--domain", required=True)
    ap.add_argument("--log", type=Path, required=True)
    ap.add_argument("--evidence", type=Path, required=True)
    ap.add_argument("--checks", type=Path, required=True)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20260926)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    evidence = json.loads(args.evidence.read_text())
    check_fns = load_check_fns(args.checks)
    rows = [json.loads(line) for line in args.log.read_text().splitlines() if line.strip()]

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(args.sopbench))
    from grade_judge_gate import load_tasks, truth_of
    from sopbench_judge_gate import task_leaves

    cwd = os.getcwd()
    os.chdir(args.sopbench)
    tasks = load_tasks(args.sopbench, args.domain)
    os.chdir(cwd)

    rng = random.Random(args.seed)
    sample = rng.sample(rows, min(args.n, len(rows)))

    out = []
    for r in sample:
        constraint, polarity, tk = r["constraint"], r["polarity"], r.get("task_key")
        entry = {"task_key": tk, "goal": r.get("goal"), "constraint": constraint,
                 "polarity": polarity, "condition": r.get("condition")}
        if tk not in tasks:
            entry["error"] = "no_task_key"
            out.append(entry)
            continue
        _pos, task = tasks[tk]
        name = constraint if polarity else f"not {constraint}"
        os.chdir(args.sopbench)
        leaf = task_leaves(task.get("constraints")).get(name)
        t = truth_of(args.domain, task, name, leaf[2] if leaf else {})
        os.chdir(cwd)
        ev = evidence.get(tk, {})
        resolved_params = resolve_params(leaf[2] if leaf else None, task)
        fn = check_fns.get(constraint)
        try:
            raw = fn(ev.get("tool_results", {}), resolved_params) if fn else None
        except Exception as exc:
            raw = f"EXCEPTION {type(exc).__name__}: {exc}"
        predicted = None
        if isinstance(raw, bool):
            predicted = raw if polarity else (not raw)
        entry.update({
            "tool_results_seen": ev.get("tool_results", {}),
            "resolved_params_seen": resolved_params,
            "check_raw_output": raw,
            "check_predicted_stated_condition": predicted,
            "sopbench_truth_of": t,
            "match": (predicted == t) if isinstance(t, bool) and predicted is not None else None,
        })
        out.append(entry)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2, default=str))
    n_match = sum(1 for e in out if e.get("match") is True)
    n_mismatch = sum(1 for e in out if e.get("match") is False)
    print(f"wrote {len(out)} rows to {args.out}: {n_match} match, {n_mismatch} mismatch, "
          f"{len(out) - n_match - n_mismatch} abstain/ungradable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
