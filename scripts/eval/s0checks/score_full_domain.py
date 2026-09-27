#!/usr/bin/env python3
"""Score generated S0 precondition checkers against EVERY constraint leaf of
EVERY task in a domain's own `data/<domain>_tasks.json`, using SOPBench's own
ground truth (`grade_judge_gate.truth_of`).

This is the `library` transfer twin of `score_hotel.py`. It differs from
`score_hotel.py` only in where the (task, constraint, polarity) rows to grade
come from: `score_hotel.py` reads them off a prior judge's log
(`jev.verdicts.jsonl`, the only value-gate log this programme has); this
script has no such log for `library`; it enumerates every leaf of every
task directly (this is a superset of what a judge log would contain, not an
approximation of it -- see PLAN.md's "Idea 3" section for why this is the
right substitute for the ill-fitting 270-cell dirgraph sweep).

A leaf is skippable (not scored) when:
  - its ground truth is ungradable (`truth_of` returns a non-bool), same
    convention as `grade_judge_gate.grade()`, or
  - the task's `task_key` has no reconstructed evidence (no trajectory
    covered that task).

Usage:
    python3 score_full_domain.py --domain library \
        --evidence evals/s0checks/library/evidence.json \
        --checks evals/s0checks/library/checks \
        --out evals/s0checks/library/score.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from collections import Counter
from pathlib import Path


def resolve_params(leaf_arg_map: dict | None, task: dict) -> dict:
    """See score_hotel.py's `resolve_params` -- identical, kept in sync by hand
    (both are tiny; a shared import would cost more than it saves here)."""
    constraint_parameters = task.get("constraint_parameters") or {}
    user_known = task.get("user_known") or {}
    resolved = dict(constraint_parameters)
    for func_param, src_key in (leaf_arg_map or {}).items():
        if isinstance(src_key, str) and src_key.startswith("value "):
            try:
                resolved[func_param] = eval(src_key[len("value "):])  # noqa: S307
            except Exception:
                continue
        elif src_key in user_known:
            resolved[func_param] = user_known[src_key]
        elif src_key in constraint_parameters:
            resolved[func_param] = constraint_parameters[src_key]
    return resolved


def load_check_fns(checks_dir: Path) -> dict:
    fns = {}
    for py in sorted(checks_dir.glob("*.py")):
        name = py.stem
        spec = importlib.util.spec_from_file_location(f"s0check_{name}", py)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not hasattr(mod, "check"):
            print(f"WARNING {py}: no `check` function defined, skipping")
            continue
        fns[name] = mod.check
    return fns


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sopbench", type=Path, default=Path.home() / "Code" / "SOPBench")
    ap.add_argument("--domain", required=True)
    ap.add_argument("--evidence", type=Path, required=True)
    ap.add_argument("--checks", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    evidence = json.loads(args.evidence.read_text())
    check_fns = load_check_fns(args.checks)

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/eval
    sys.path.insert(0, str(args.sopbench))
    from grade_judge_gate import load_tasks, truth_of
    from sopbench_judge_gate import task_leaves

    cwd = os.getcwd()
    os.chdir(args.sopbench)
    tasks = load_tasks(args.sopbench, args.domain)
    os.chdir(cwd)

    per: dict[str, Counter] = {}
    ungradable = Counter()
    total = Counter()
    worst_examples: dict[str, list] = {}

    for tk, (_pos, task) in tasks.items():
        ev = evidence.get(tk)
        os.chdir(args.sopbench)
        leaves = task_leaves(task.get("constraints"))
        os.chdir(cwd)
        for name, leaf in leaves.items():
            polarity = not name.startswith("not ")
            constraint = name[4:] if not polarity else name
            os.chdir(args.sopbench)
            t = truth_of(args.domain, task, name, leaf[2] if leaf else {})
            os.chdir(cwd)
            if not isinstance(t, bool):
                ungradable[t.split(":")[1].strip() if isinstance(t, str) and ":" in t else str(t)] += 1
                total["ungradable"] += 1
                continue
            total["gradable"] += 1

            c = per.setdefault(constraint, Counter())
            c["gradable"] += 1

            fn = check_fns.get(constraint)
            if fn is None or ev is None:
                c["no_check_or_evidence"] += 1
                continue

            resolved_params = resolve_params(leaf[2] if leaf else None, task)
            try:
                raw = fn(ev["tool_results"], resolved_params)
            except Exception as exc:
                raw = None
                c[f"exception:{type(exc).__name__}"] += 1

            if raw is None:
                c["abstain"] += 1
                continue

            predicted = raw if polarity else (not raw)
            c["decided"] += 1
            total["decided"] += 1
            if predicted == t:
                c["correct"] += 1
                total["correct"] += 1
            else:
                c["wrong"] += 1
                total["wrong"] += 1
                worst_examples.setdefault(constraint, []).append(
                    {"task_key": tk, "goal": task.get("user_goal"), "polarity": polarity,
                     "predicted": predicted, "truth": t})
            cell = ("pred_block" if not predicted else "pred_pass") + ("_correct" if predicted == t else "_wrong")
            c[cell] += 1
            total[cell] += 1

    per_out = {}
    for name, c in per.items():
        gradable = c["gradable"]
        decided = c["decided"]
        correct = c["correct"]
        per_out[name] = {
            "gradable_rows": gradable,
            "decided": decided,
            "coverage": round(decided / gradable, 3) if gradable else None,
            "accuracy_on_decided": round(correct / decided, 3) if decided else None,
            "abstain": c["abstain"],
            "wrong": c["wrong"],
            "pred_block_correct": c["pred_block_correct"],
            "pred_block_wrong": c["pred_block_wrong"],
            "pred_pass_correct": c["pred_pass_correct"],
            "pred_pass_wrong": c["pred_pass_wrong"],
        }

    blocks = total["pred_block_correct"] + total["pred_block_wrong"]
    passes = total["pred_pass_correct"] + total["pred_pass_wrong"]
    held = total["pred_pass_correct"] + total["pred_block_wrong"]
    not_held = total["pred_block_correct"] + total["pred_pass_wrong"]
    aggregate = {
        "gradable_rows": total["gradable"],
        "decided": total["decided"],
        "coverage": round(total["decided"] / total["gradable"], 3) if total["gradable"] else None,
        "accuracy_on_decided": round(total["correct"] / total["decided"], 3) if total["decided"] else None,
        "blocks": blocks, "blocks_correct": total["pred_block_correct"], "blocks_wrong": total["pred_block_wrong"],
        "passes": passes, "passes_wrong_let_through": total["pred_pass_wrong"],
        "TPR": round(total["pred_block_correct"] / not_held, 3) if not_held else None,
        "FPR": round(total["pred_block_wrong"] / held, 3) if held else None,
        "lift": (round(total["pred_block_correct"] / not_held - total["pred_block_wrong"] / held, 3)
                 if held and not_held else None),
        "ungradable_reasons": dict(ungradable),
    }

    with_wrong = [(k, v) for k, v in per_out.items() if v["wrong"] > 0]
    ranked = sorted(with_wrong, key=lambda kv: (-kv[1]["wrong"], kv[1]["accuracy_on_decided"] or 0))
    zero_coverage = [k for k, v in per_out.items() if v["decided"] == 0 and v["gradable_rows"] > 0]
    worst = ranked[:3] if ranked else [(k, per_out[k]) for k in zero_coverage[:3]]
    out = {"per_precondition": per_out, "aggregate": aggregate,
           "worst_3_preconditions": [w[0] for w in worst],
           "zero_coverage_preconditions": zero_coverage,
           "worst_examples": {k: v[:5] for k, v in worst_examples.items()}}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps(aggregate, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
