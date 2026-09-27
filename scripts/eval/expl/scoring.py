"""Per-task scoring, same semantics as evals/sopbench-bank-asop/score_asop_arms.py.

Same evaluator (`env.evaluator.evaluator_function_directed_graph`), same
`try_eval` parsing, same six METRICS — so a delta measured here means what a
delta measured there means. This module does NOT import that script directly:
its SUBSET/DOMAIN/MODEL globals are cached at import time from environment
variables, which is fine for a one-shot CLI but wrong for an orchestrator that
scores several different subsets (EDIT/SELECT/TEST) of several different
candidate runs in one process. Reimplementing the ~30-line scoring body here
(exactly as `score_asop_arms.py`'s own docstring says IT does relative to
`score_ladder.py`, for the same reason) keeps the semantics identical without
the import-time coupling.

Requires the SOPBench checkout importable — run under
`~/Code/SOPBench/.venv/bin/python`.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

DEFAULT_SOPBENCH = Path.home() / "Code" / "SOPBench"

METRICS = [
    "success",
    "dirgraph_satisfied",
    "constraint_not_violated",
    "database_match",
    "action_called_correctly",
    "no_tool_call_error",
]

# EXP-L.md's failure-conjunct names, in the priority order used to pick ONE
# "the failed conjunct" for a task with more than one false metric — fixed and
# documented, not a per-run choice, so grouping is reproducible.
CONJUNCT_PRIORITY: list[tuple[str, str]] = [
    ("dirgraph", "dirgraph_satisfied"),
    ("constraint", "constraint_not_violated"),
    ("database", "database_match"),
    ("action_called_correctly", "action_called_correctly"),
    ("tool_error", "no_tool_call_error"),
]

PLACEHOLDER_KEY = "_shard_placeholder"


def _sopbench_imports(sopbench: Path):
    if str(sopbench) not in sys.path:
        sys.path.insert(0, str(sopbench))
    from env.evaluator import evaluator_function_directed_graph  # noqa: E402
    from run_evaluation import try_eval  # noqa: E402

    return evaluator_function_directed_graph, try_eval


def score_file(
    path: Path,
    subset_positions: Optional[set] = None,
    sopbench: Path = DEFAULT_SOPBENCH,
) -> dict:
    """Per-task record keyed by task POSITION (the file's own index).

    `subset_positions`, when given, is checked BEFORE `sim["interactions"]` is
    ever touched — positions a `--task-ids` run does not own hold shard
    placeholders (`sopbench_shard.PLACEHOLDER_KEY`), not real results, and the
    placeholder's shape does not survive the evaluator call. Mirrors
    `score_asop_arms.py`'s `ASOP_SUBSET` gate, which exists for exactly this.
    """
    evaluator_function_directed_graph, try_eval = _sopbench_imports(sopbench)
    sims = json.loads(Path(path).read_text())
    out: dict = {}
    for idx, sim in enumerate(sims):
        if subset_positions is not None and idx not in subset_positions:
            continue
        interactions = sim.get("interactions") or []
        if not interactions:
            continue
        interaction_log = interactions[0]
        if interaction_log.get(PLACEHOLDER_KEY):
            continue
        results = {"final_database": interaction_log["database"]}
        interaction = interaction_log["interaction"]
        func_calls = []
        for i in range(len(interaction) - 1):
            calls = list(interaction[i].get("tool_calls") or [])
            calls = [tc for tc in calls if tc["function"]["name"].lower() not in ("n/a", "na", "none", "null")]
            if calls:
                func_calls.append(
                    {
                        "tool_name": interaction[i + 1]["tool_name"],
                        "arguments": try_eval(calls[0]["function"]["arguments"]),
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
        rec = {m: bool(ev[m]) for m in METRICS}
        rec["action_should_succeed"] = bool(sim["task"]["action_should_succeed"])
        rec["num_function_calls"] = len(func_calls)
        rec["user_goal"] = sim["task"].get("user_goal")
        rec["user_prompt"] = sim["task"].get("user_prompt") or sim["task"].get("user_instruction") or ""
        rec["tool_calls"] = func_calls
        out[idx] = rec
    return out


def failed_conjunct(rec: dict) -> Optional[str]:
    """The first (by CONJUNCT_PRIORITY) false metric, or None if `rec["success"]`."""
    if rec["success"]:
        return None
    for label, metric in CONJUNCT_PRIORITY:
        if not rec[metric]:
            return label
    # success is False but every named conjunct is True: SOPBench's `success`
    # is not a pure AND of these six (edge case, still worth a bucket rather
    # than a silent drop).
    return "other"


def is_refusal(rec: dict) -> bool:
    """Zero substantive tool calls — the same proxy `score_asop_arms.py` uses
    for "cannot act, collects should-refuse successes for free" (its
    NO_PROCEDURE_GOALS comment)."""
    return rec["num_function_calls"] == 0


def summarize(records: dict) -> dict:
    """Aggregate stats over a set of scored task records: success rate,
    refusal rate, and the permissible/impermissible (should-succeed vs
    should-refuse) split RL review #2 asked to track.
    """
    n = len(records)
    if n == 0:
        return {"n": 0}
    perm = [r for r in records.values() if r["action_should_succeed"]]
    imp = [r for r in records.values() if not r["action_should_succeed"]]
    return {
        "n": n,
        "success_rate": sum(r["success"] for r in records.values()) / n,
        "refusal_rate": sum(is_refusal(r) for r in records.values()) / n,
        "n_permissible": len(perm),
        "success_rate_permissible": (sum(r["success"] for r in perm) / len(perm)) if perm else None,
        "n_impermissible": len(imp),
        "success_rate_impermissible": (sum(r["success"] for r in imp) / len(imp)) if imp else None,
    }


def failure_groups(records: dict) -> dict:
    """Failed tasks (success is False), grouped by `failed_conjunct`."""
    groups: dict = {}
    for pos, rec in records.items():
        if rec["success"]:
            continue
        label = failed_conjunct(rec)
        groups.setdefault(label, []).append((pos, rec))
    return groups
