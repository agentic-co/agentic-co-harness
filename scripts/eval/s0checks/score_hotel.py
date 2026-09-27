#!/usr/bin/env python3
"""Score the generated S0 precondition checkers against the 958 live hotel
gate rows, using SOPBench's own ground truth (grade_judge_gate.truth_of).

Unlike generate_checks.py, THIS script is allowed to see everything: the
logged rows, the reconstructed tool evidence, and SOPBench's ground truth.
The checks it scores were written before any of this was in scope (see
evals/s0checks/hotel/generation/*.json for the exact generation prompts, which
never touch this file's inputs).

For each row:
  - constraint = row["constraint"], polarity = row["polarity"]
  - name = constraint if polarity else f"not {constraint}"  (matches
    grade_judge_gate.grade()'s own convention)
  - ground truth = truth_of(domain, task, name, leaf_arg_map); rows where this
    can't be computed are "ungradable" and excluded, exactly as in
    grade_judge_gate.grade().
  - predicted_raw = check_fns[constraint](tool_results, params) -- the
    GENERATED function's answer to the RAW (un-negated) constraint.
  - predicted_stated = predicted_raw if polarity else (not predicted_raw),
    i.e. same polarity handling as ground truth, so predicted_stated and
    ground truth are directly comparable. predicted_raw is None -> abstain,
    counted separately (not "wrong").

Usage (run from anywhere; the script chdirs internally for SOPBench imports):
    python3 score_hotel.py --domain hotel \
        --log <archive>/jev.verdicts.jsonl \
        --evidence evals/s0checks/hotel/evidence.json \
        --checks evals/s0checks/hotel/checks \
        --out evals/s0checks/hotel/score.json
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
    """Build the exact `params` a generated check() should see for ONE
    precondition instance, mirroring `Dependency_Evaluator._single`'s own
    argument resolution (env/dep_eval.py) -- not a flat per-task dict.

    A constraint FUNCTION's parameter name (e.g. "check_in_date") is bound,
    per LEAF, to a key in the task's user_known (e.g. "old_check_in_date" for
    `has_confirmed_reservation` inside `modify_reservation`, vs
    "check_in_date" for the same function inside `cancel_reservation`).
    Resolving once per task and reusing it for every constraint silently
    hands every leaf the WRONG date pair whenever a domain reuses one
    predicate under two different argument bindings -- this is exactly what
    made `has_confirmed_reservation` score 0.442 before this fix (n8 in the
    generation notes / PLAN.md). Dependency parameters (min_age,
    modification_deadline_hours, valid_document_types, ...) are not part of
    any arg_map -- they're bound directly to the state tracker instance -- so
    they're included under their own canonical names unconditionally.
    """
    constraint_parameters = task.get("constraint_parameters") or {}
    user_known = task.get("user_known") or {}
    resolved = dict(constraint_parameters)
    for func_param, src_key in (leaf_arg_map or {}).items():
        if isinstance(src_key, str) and src_key.startswith("value "):
            try:
                resolved[func_param] = eval(src_key[len("value "):])  # noqa: S307 - SOPBench's own convention
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
    ap.add_argument("--log", type=Path, required=True)
    ap.add_argument("--evidence", type=Path, required=True)
    ap.add_argument("--checks", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--legacy-flat-params", action="store_true",
                    help="AUDIT ONLY: reproduce the pre-fix behavior (one flat "
                         "user_known+constraint_parameters dict per TASK, not "
                         "resolved per-leaf via arg_map) -- exists solely so the "
                         "before/after delta from the param-resolution fix can be "
                         "regenerated on demand against the same frozen check files.")
    args = ap.parse_args()

    evidence = json.loads(args.evidence.read_text())
    check_fns = load_check_fns(args.checks)
    rows = [json.loads(line) for line in args.log.read_text().splitlines() if line.strip()]

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

    for r in rows:
        constraint = r["constraint"]
        polarity = r["polarity"]
        tk = r.get("task_key")
        if tk not in tasks:
            total["no_task_key"] += 1
            continue
        _pos, task = tasks[tk]
        name = constraint if polarity else f"not {constraint}"
        leaf = task_leaves(task.get("constraints")).get(name)
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
        ev = evidence.get(tk)
        if fn is None or ev is None:
            c["no_check_or_evidence"] += 1
            continue

        resolved_params = ev["params"] if args.legacy_flat_params else resolve_params(leaf[2] if leaf else None, task)
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
                {"task_key": tk, "goal": r.get("goal"), "polarity": polarity,
                 "predicted": predicted, "truth": t, "condition": r.get("condition")})
        # block/pass bookkeeping, mirroring grade_judge_gate.grade()
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
        "blocks": blocks,
        "blocks_correct": total["pred_block_correct"],
        "blocks_wrong": total["pred_block_wrong"],
        "passes": passes,
        "passes_wrong_let_through": total["pred_pass_wrong"],
        "TPR": round(total["pred_block_correct"] / not_held, 3) if not_held else None,
        "FPR": round(total["pred_block_wrong"] / held, 3) if held else None,
        "lift": (round(total["pred_block_correct"] / not_held - total["pred_block_wrong"] / held, 3)
                 if held and not_held else None),
        "ungradable_reasons": dict(ungradable),
        "no_task_key": total["no_task_key"],
    }
    jev_comparison = {
        "jev_blocks": 250, "jev_blocks_wrong": 81, "jev_passes_wrong_let_through": 3, "jev_lift": 0.88,
        "s0_blocks": blocks, "s0_blocks_wrong": aggregate["blocks_wrong"],
        "s0_passes_wrong_let_through": aggregate["passes_wrong_let_through"], "s0_lift": aggregate["lift"],
    }

    # "Worst" = most actual wrong answers first (not accuracy alone, which ties
    # every 100%-correct-but-high-volume precondition with the genuinely bad
    # ones); zero-coverage preconditions (abstain on everything) are a
    # different failure mode and are called out separately, not ranked here
    # unless nothing else has any wrong answers at all.
    with_wrong = [(k, v) for k, v in per_out.items() if v["wrong"] > 0]
    ranked = sorted(with_wrong, key=lambda kv: (-kv[1]["wrong"], kv[1]["accuracy_on_decided"] or 0))
    zero_coverage = [k for k, v in per_out.items() if v["decided"] == 0 and v["gradable_rows"] > 0]
    worst = ranked[:3] if ranked else [(k, per_out[k]) for k in zero_coverage[:3]]

    out = {"per_precondition": per_out, "aggregate": aggregate, "jev_comparison": jev_comparison,
           "worst_3_preconditions": [w[0] for w in worst],
           "zero_coverage_preconditions": zero_coverage,
           "worst_examples": {k: v[:5] for k, v in worst_examples.items()}}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps({"aggregate": aggregate, "jev_comparison": jev_comparison}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
