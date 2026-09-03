"""Cycle triage — a cheap model decides what runs this heartbeat.

Two-brain execution: a triage LM (default Claude Haiku, swappable to any
OpenAI-compatible endpoint via config) walks the open task list each cycle
and proposes {run_now | defer | needs_human} with a priority order. The
frontier model only runs when real work happens.

Triage is advisory, never load-bearing for safety:
- If the triage LM is down or returns garbage, the cycle falls back to
  "run everything in queue order" with a WARNING.
- verify_child tasks always run, whatever triage says — triage-model
  downtime must never block verification.
- Tasks triage forgot to mention run anyway; only an explicit defer or
  needs_human holds a task back, and that is logged per task.
"""

from __future__ import annotations

import json

import dspy

from .beads import Task
from .signatures import TriageCycle


def _is_verify(task: Task) -> bool:
    return task.metadata.get("type") == "verify_child"


def _summarize(tasks: list[Task]) -> str:
    """Build the triage input JSON for the open queue.

    Carries each task's assignment context so triage can reason about who a task
    is (or is proposed to be) for: `assigned_to` (Stage 1 human/agent assignee, if
    that field exists yet) and `proposed_assigned_to` (a planner proposal that has
    not been applied). Both degrade to null when absent.
    """
    return json.dumps(
        [
            {
                "id": t.id,
                "title": t.title,
                "agent": t.assigned_agent,
                "priority": t.priority.value,
                "created_at": t.created_at,
                # getattr keeps zero file-level dependency on Stage 1's assigned_to
                # field — it reads as None until that stage lands.
                "assigned_to": getattr(t, "assigned_to", None),
                "proposed_assigned_to": t.metadata.get("proposed_assigned_to"),
            }
            for t in tasks
        ]
    )


def triage_order(tasks: list[Task], lm) -> list[Task]:
    """Return the tasks to execute this cycle, in execution order.

    Raises on any LM failure — the caller owns the loud fallback so the
    WARNING is printed exactly once at the cycle boundary.
    """
    summary = _summarize(tasks)
    with dspy.context(lm=lm):
        decision = dspy.ChainOfThought(TriageCycle)(open_tasks=summary)

    by_id = {t.id: t for t in tasks}
    valid_run = [tid for tid in decision.run_now if tid in by_id]
    held = {
        tid
        for tid in list(decision.defer) + list(decision.needs_human)
        if tid in by_id and tid not in set(valid_run)
    }

    ordered = [by_id[tid] for tid in dict.fromkeys(valid_run)]
    mentioned = set(valid_run) | held
    # Unmentioned tasks run anyway, in queue order — triage can prioritize
    # and hold, but it cannot silently drop work.
    ordered += [t for t in tasks if t.id not in mentioned]

    for tid in held:
        task = by_id[tid]
        if _is_verify(task):
            # verify_child is never deferrable.
            ordered.append(task)
            print(
                f"[triage] {tid} ({task.title}) is a verify_child task — "
                f"overriding triage hold, it runs this cycle"
            )
        else:
            bucket = "needs_human" if tid in set(decision.needs_human) else "defer"
            print(f"[triage] {tid} ({task.title}) held this cycle ({bucket})")

    # needs_planner is ADVISORY: surface the IDs triage thinks deserve planner
    # decomposition/routing, but NEVER auto-spawn a planner bead — capable-model
    # attention is budgeted and the planner runs only when explicitly created.
    # Absent/garbage output degrades to nothing surfaced (defensive getattr +
    # type guard); it never changes execution order.
    raw_planner = getattr(decision, "needs_planner", None)
    if isinstance(raw_planner, list):
        for tid in dict.fromkeys(raw_planner):
            if tid in by_id:
                print(
                    f"[triage] {tid} ({by_id[tid].title}) flagged needs_planner "
                    f"— surfaced for a human to route; NOT auto-spawning a planner"
                )
    elif raw_planner is not None:
        # Present but the wrong shape (e.g. a bare string). Degrading silently
        # would hide a triage-model contract drift; warn so it is visible.
        print(
            f"[triage] WARNING: needs_planner is not a list "
            f"({type(raw_planner).__name__}={raw_planner!r}) — ignoring it"
        )

    return ordered
