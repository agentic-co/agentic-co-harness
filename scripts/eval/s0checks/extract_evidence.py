#!/usr/bin/env python3
"""Reconstruct per-task tool evidence for the S0-checks eval (idea 3: "System 2
writes System 0" -- see ai-tasks/local-s1s2/PLAN.md).

jev.verdicts.jsonl's own `result` field is truncated to 200 chars (it exists
only so a human can eyeball the log). The judge that produced those verdicts
actually saw the FULL tool history, so grading a from-scratch deterministic
checker against the truncated string would be an unfair, lossier comparison.
This script replays the untruncated conversation trajectories SOPBench logged
(`.../confirm/gate/jev/hotel/*.json`) and, for each task_key, reconstructs:

  - "params": the task's own user_known + constraint_parameters, flattened
    into one dict (arg_maps in this domain are identity mappings: a
    constraint function's parameter name equals the key name in user_known /
    constraint_parameters, verified by inspection of hotel_assistant.py's
    constraint_dependencies).
  - "tool_results": every tool call's parsed return value, keyed by tool name
    (last call wins if a tool is called more than once -- true state doesn't
    change until the single goal action fires, so this is exact, not a guess,
    for this benchmark's single-goal-per-task tasks).

Nothing here is ground truth: it is exactly the evidence an assistant/gate
would have accumulated by conversation end. Ground truth is computed
separately (grade_judge_gate.truth_of) only for SCORING, never fed to the
check-function generator.

Usage:
    python3 extract_evidence.py --domain hotel \
        --traj ~/Code/agentco-harness-eval-archive/2026-09-24-session/scratchpad/confirm/gate/jev/hotel/*.json \
        --out evals/s0checks/hotel/evidence.json
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path


def parse_tool_content(content: str):
    """Best-effort inverse of `str(return_value)`. Falls back to the raw
    string (correct for the timestamp / room-id / free-text cases, which are
    exactly the tool outputs this domain returns as plain strings)."""
    try:
        return ast.literal_eval(content)
    except (ValueError, SyntaxError):
        return content


def extract_task(rec: dict) -> dict:
    task = rec["task"]
    params = {**(task.get("constraint_parameters") or {}), **(task.get("user_known") or {})}
    tool_results: dict = {}
    for interaction in rec.get("interactions", []):
        for msg in interaction.get("interaction", []):
            if "tool_name" in msg and "content" in msg:
                tool_results[msg["tool_name"]] = parse_tool_content(msg["content"])
    return {"user_goal": task.get("user_goal"), "params": params, "tool_results": tool_results}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sopbench", type=Path, default=Path.home() / "Code" / "SOPBench")
    ap.add_argument("--domain", required=True)
    ap.add_argument("--traj", type=Path, required=True, help="trajectory JSON file")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    sys.path.insert(0, str(args.sopbench))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/eval
    import os
    cwd = os.getcwd()
    os.chdir(args.sopbench)
    from sopbench_judge_gate import task_key
    os.chdir(cwd)

    records = json.loads(args.traj.read_text())
    out = {}
    for rec in records:
        if rec.get("domain") != args.domain:
            continue
        os.chdir(args.sopbench)
        k = task_key(rec["task"])
        os.chdir(cwd)
        out[k] = extract_task(rec)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2, default=str))
    print(f"wrote {len(out)} tasks to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
