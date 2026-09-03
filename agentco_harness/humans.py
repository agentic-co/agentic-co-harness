"""Human-executor task operations shared by the CLI and the Telegram webhook.

Stage 1 of the delegation layer treats a person as a first-class executor:
a task with ``assigned_to = "human:<name>"`` waits (visible in ``agentco me``),
is never dispatched to a model, and is completed / declined / snoozed by the
human. This module holds the small pieces of that lifecycle that BOTH the CLI
(`agentco tasks ...`) and the Telegram webhook (`server.py`) drive, so the two
surfaces can never diverge in behaviour.

Every accepted transition logs loudly — nothing is silently dropped.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .beads import Beads, Task, TaskStatus
from .config import Config
from .recurring import parse_duration

HUMAN_PREFIX = "human:"

# The only statuses on which a human may `done` or `decline` a task: it must be
# genuinely open work the person still owns. PENDING_APPROVAL is excluded (a
# `done` there would bypass the approval gate); DONE is excluded (never resurrect
# finished work); IN_PROGRESS/FAILED/SKIPPED are not states a waiting human task
# should be actioned from through these surfaces.
_ACTIONABLE_HUMAN_STATUSES = (TaskStatus.PENDING, TaskStatus.BLOCKED)


class TaskStateError(Exception):
    """A human-task command targeted a task in the wrong ownership or status.

    Raised (not swallowed) so every surface refuses LOUDLY with a clear reason:
    the CLI prints it and exits non-zero, the Telegram handler replies it into
    the chat. Distinct from a missing task (None) — this is a live task the
    operation is not allowed to touch.
    """


def is_human_assigned(task: Task) -> bool:
    """True when a task is owned by a human executor."""
    return isinstance(task.assigned_to, str) and task.assigned_to.startswith(HUMAN_PREFIX)


def _require_human(task: Task, action: str) -> None:
    """Refuse loudly unless ``task`` is human-assigned."""
    if not is_human_assigned(task):
        raise TaskStateError(
            f"{action}: {task.id} is not human-assigned (assigned_to="
            f"{task.assigned_to!r}) — refusing to {action} a task no person owns"
        )


def _require_actionable_human(task: Task, action: str) -> None:
    """Refuse loudly unless ``task`` is human-assigned AND open (pending/blocked)."""
    _require_human(task, action)
    if task.status not in _ACTIONABLE_HUMAN_STATUSES:
        allowed = "/".join(s.value for s in _ACTIONABLE_HUMAN_STATUSES)
        raise TaskStateError(
            f"{action}: {task.id} is {task.status.value}, not open ({allowed}) "
            f"— refusing (would bypass a gate or resurrect finished work)"
        )


def push_assignment(config: Config, task: Task) -> None:
    """Best-effort assignment notification (Telegram if configured).

    Pull-only `me` is too weak for the portfolio's scarcest resource, so an
    assignment is pushed. Advisory: a notification failure warns and is
    swallowed — it must never fail the assignment that triggered it.
    """
    from .notify import notify_event

    if is_human_assigned(task):
        name = task.assigned_to.split(":", 1)[1] or task.assigned_to
    else:
        name = task.assigned_to or "?"
    message = (
        f"📋 AgentCo [{config.instance_name}]: task assigned to {name} — "
        f"[{task.id}] {task.title}"
    )
    try:
        delivered = notify_event(config.notify, message, urgent=False)
        if not delivered:
            print(
                f"[humans] assignment for {task.id} not pushed "
                f"(no channel accepted it) — visible in `agentco me`"
            )
    except Exception as e:  # noqa: BLE001 — a broken channel never fails assignment
        print(f"[humans] WARNING: assignment push failed for {task.id}: {e}")


def decline_task(beads: Beads, task_id: str, reason: str | None = None) -> Task | None:
    """Return a human-assigned task to the queue, unassigned.

    Clears ``assigned_to`` through the explicit ``allow_human_reassign`` path
    (the only sanctioned way past the human-lineage invariant), appends the
    reason to metadata, and resets status to pending. Returns None if the task
    does not exist. Raises TaskStateError if the task is not a human-assigned,
    open (pending/blocked) task — declining anything else would return an
    agent task to the queue or resurrect finished/gated work.
    """
    task = beads.get(task_id)
    if task is None:
        return None
    _require_actionable_human(task, "decline")
    metadata = dict(task.metadata)
    history = list(metadata.get("decline_history", []))
    history.append(
        {"reason": reason or "", "at": datetime.now(timezone.utc).isoformat()}
    )
    metadata["decline_history"] = history
    updated = beads.update(
        task_id,
        assigned_to=None,
        status=TaskStatus.PENDING,
        metadata=metadata,
        allow_human_reassign=True,
    )
    print(
        f"[humans] DECLINE: {task_id} returned to queue (was {task.assigned_to!r})"
        + (f" — reason: {reason}" if reason else "")
    )
    return updated


def snooze_task(
    beads: Beads, task_id: str, interval: str, now: datetime | None = None
) -> Task | None:
    """Hide a task from `me` until ``now + interval`` (e.g. ``2d``).

    Stores an ISO ``snoozed_until`` timestamp in metadata that me.py's
    collection branches respect. Raises ValueError on an unparseable interval
    (surfaced loudly by the caller). Returns None if the task does not exist.
    Raises TaskStateError if the task is not human-assigned — snooze is a human
    triage action on the person's own queue, in ANY status the item may hold
    (a failed or needs-input human task is snoozable), so it carries only the
    ownership guard, not the pending/blocked status guard that done/decline use.
    """
    now = now or datetime.now(timezone.utc)
    until = now + parse_duration(interval)  # ValueError on bad interval
    task = beads.get(task_id)
    if task is None:
        return None
    _require_human(task, "snooze")
    metadata = dict(task.metadata)
    metadata["snoozed_until"] = until.isoformat()
    updated = beads.update(task_id, metadata=metadata)
    print(f"[humans] SNOOZE: {task_id} hidden until {until.isoformat()} (+{interval})")
    return updated


def handle_telegram_command(
    beads: Beads, config: Config, text: str, now: datetime | None = None
) -> str | None:
    """Parse and apply a Telegram command from a message body.

    Recognized commands (case-insensitive first token):
      - ``done <id>``            — mark complete; idempotent on an already-done task
      - ``decline <id> [reason]``— return to queue unassigned
      - ``snooze <id> <interval>``— hide from `me` for the interval

    Returns a short acknowledgement string for a recognized command (even a
    no-op / error, so the caller can reply), or None if the text is not a
    command this handler owns. Every accepted STATE transition logs loudly.
    """
    parts = text.strip().split()
    if not parts:
        return None
    cmd = parts[0].lower()

    if cmd == "done":
        if len(parts) < 2:
            return "done: missing task id"
        task_id = parts[1]
        task = beads.get(task_id)
        if task is None:
            return f"done: task {task_id} not found"
        if not is_human_assigned(task):
            print(f"[telegram] REFUSED done: {task_id} not human-assigned")
            return (
                f"done: {task_id} is not human-assigned — refusing "
                f"(only the human executor's own tasks close via `done`)"
            )
        if task.status == TaskStatus.DONE:
            # Idempotent — acknowledge, no state change, no error.
            return f"done: {task_id} already complete (no change)"
        if task.status not in _ACTIONABLE_HUMAN_STATUSES:
            allowed = "/".join(s.value for s in _ACTIONABLE_HUMAN_STATUSES)
            print(f"[telegram] REFUSED done: {task_id} is {task.status.value}")
            return (
                f"done: {task_id} is {task.status.value}, not open ({allowed}) "
                f"— refusing (would bypass approval or resurrect finished work)"
            )
        beads.complete(task_id, result=None)
        print(f"[telegram] DONE: {task_id} ({task.title}) marked complete")
        return f"done: {task_id} marked complete"

    if cmd == "decline":
        if len(parts) < 2:
            return "decline: missing task id"
        task_id = parts[1]
        reason = " ".join(parts[2:]) or None
        try:
            task = decline_task(beads, task_id, reason)
        except TaskStateError as e:
            print(f"[telegram] REFUSED decline: {e}")
            return f"decline: refused — {e}"
        if task is None:
            return f"decline: task {task_id} not found"
        return f"decline: {task_id} returned to queue"

    if cmd == "snooze":
        if len(parts) < 3:
            return "snooze: usage: snooze <id> <interval> (e.g. 2d)"
        task_id = parts[1]
        interval = parts[2]
        try:
            task = snooze_task(beads, task_id, interval, now=now)
        except ValueError as e:
            return f"snooze: bad interval ({e})"
        except TaskStateError as e:
            print(f"[telegram] REFUSED snooze: {e}")
            return f"snooze: refused — {e}"
        if task is None:
            return f"snooze: task {task_id} not found"
        return f"snooze: {task_id} snoozed for {interval}"

    return None
