"""System Review — the artifact a closed goal leaves behind.

When a goal bead reaches DONE, the plan that produced it is the most valuable
thing in the store and the shortest-lived: within a week nobody remembers which
children were planned up front, which were fix beads bolted on when reality
disagreed, or which gates actually ran. This module writes that down, once, at
the moment the goal closes.

Two properties it refuses to give up:

* **Deterministic.** Pure bead data — titles, statuses, verify payloads,
  timestamps. No LLM, no paraphrase, no judgment. A review you have to
  re-verify is not evidence.
* **Never fails the completion.** Generation is best-effort and warns on
  stderr. A goal that did the work is DONE whether or not its paperwork
  rendered; the reverse — refusing to close proven work over a failed file
  write — would be the tail wagging the dog.

Divergence (`metadata.divergence`) is the review's sharpest column. `good` means
the PLAN was wrong and the builder found a better path — that feeds PRIME and
the plan templates. `bad` means execution cut a corner the plan did not
sanction — that feeds RCA. Untagged is the honest default; nothing is inferred.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from .beads import Task, TaskStatus, gate_kind, verify_check_text

REVIEWS_DIRNAME = "reviews"

# What marks a child as a fix bead — i.e. work that was NOT in the plan and got
# added because the plan met reality. Both existing conventions count, plus the
# RCA source, so the count means "unplanned work" rather than "work carrying one
# particular key".
FIX_BEAD_KEYS = ("fix_for", "rca_for")

DIVERGENCE_FOOTER = (
    "## Divergence convention\n"
    "\n"
    "- **good** — the PLAN was wrong: the builder found a better path than the one\n"
    "  planned. Feed this back into PRIME and the plan templates, so the next\n"
    "  decomposition starts where this one ended up.\n"
    "- **bad** — EXECUTION took a shortcut the plan did not sanction. Feed this into\n"
    "  the RCA path (`metadata.rca_for`); it is a process failure, not a discovery.\n"
    "\n"
    "Tag a bead by setting `metadata.divergence` to `good` or `bad`. Untagged means\n"
    "nobody has judged it — never that it went to plan.\n"
)


def reviews_dir(store_path: Path | str) -> Path:
    """`<store-dir>/reviews` — reviews live beside the beads they describe."""
    return Path(store_path).parent / REVIEWS_DIRNAME


def review_path(store_path: Path | str, goal_id: str) -> Path:
    return reviews_dir(store_path) / f"{goal_id}.md"


def is_goal(task: Task) -> bool:
    """A bead explicitly marked as a goal (`metadata.goal` / `metadata.is_goal`).

    Explicit only. Chronicle's looser "has children" heuristic is right for a
    daily summary but wrong here: it would generate a review for every parent
    bead that ever spawned a subtask.
    """
    metadata = task.metadata or {}
    return bool(metadata.get("goal")) or bool(metadata.get("is_goal"))


def is_fix_bead(task: Task) -> bool:
    metadata = task.metadata or {}
    if task.source == "rca":
        return True
    return any(metadata.get(key) for key in FIX_BEAD_KEYS)


def _duration(created: str | None, closed: str | None) -> str:
    """Human-readable created→closed span, or '?' when either stamp is unusable."""
    try:
        start = datetime.fromisoformat(created or "")
        end = datetime.fromisoformat(closed or "")
    except (TypeError, ValueError):
        return "?"
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    seconds = int((end - start).total_seconds())
    if seconds < 0:
        return "?"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _verify_class(task: Task) -> str:
    spec = (task.metadata or {}).get("verify") or {}
    return str(gate_kind(spec) or "—")


def _verify_outcome(task: Task) -> str:
    """What the gate actually said. Distinguishes 'no gate' from 'gate pending'."""
    spec = (task.metadata or {}).get("verify") or {}
    record = (task.metadata or {}).get("verify_result") or {}
    if not spec and not record:
        return "—"
    passed = record.get("passed")
    if passed is True:
        return "passed"
    if passed is False:
        return "FAILED"
    if task.status == TaskStatus.AWAITING_VERIFY:
        return "awaiting approval"
    return "not run"


def _cell(text: str) -> str:
    """Table-safe one-liner: pipes break markdown tables, newlines break rows."""
    return " ".join(str(text or "").split()).replace("|", "\\|")


def render_review(goal: Task, children: list[Task], now: datetime) -> str:
    """The review body. Pure function of the beads — no clock beyond `now`."""
    ordered = sorted(children, key=lambda t: (t.created_at, t.id))
    fixes = [t for t in ordered if is_fix_bead(t)]
    planned = [t for t in ordered if not is_fix_bead(t)]
    diverged = [t for t in ordered if (t.metadata or {}).get("divergence")]
    spec = (goal.metadata or {}).get("verify") or {}

    out: list[str] = [
        "---",
        f"goal: {goal.id}",
        f"closed: {now.isoformat()}",
        "source: agentco-system-review",
        "convention: pai-freshness-v1",
        "---",
        "",
        f"# System Review — {goal.title}",
        "",
        f"> Deterministic render of bead data at goal close. No LLM, no paraphrase.",
        "",
        f"- **Goal:** `{goal.id}` — {goal.title}",
        f"- **Created:** {goal.created_at}",
        f"- **Closed:** {now.isoformat()} ({_duration(goal.created_at, now.isoformat())})",
        f"- **Children:** {len(planned)} planned, {len(fixes)} fix bead(s) added later",
        f"- **Verify gate:** {_verify_class(goal)} — {_verify_outcome(goal)}",
    ]
    check_text = verify_check_text(spec)
    if check_text:
        out.append(f"- **Check:** `{_cell(check_text)}`")
    out.append("")

    if (goal.description or "").strip():
        out.extend(["## Goal", "", goal.description.strip(), ""])

    out.extend(["## Children", ""])
    if ordered:
        out.append("| # | id | title | status | verify | outcome | fix? | divergence |")
        out.append("|---|----|-------|--------|--------|---------|------|------------|")
        for i, child in enumerate(ordered, start=1):
            out.append(
                f"| {i} | `{child.id}` | {_cell(child.title)} | "
                f"{child.status.value} | {_verify_class(child)} | "
                f"{_verify_outcome(child)} | {'yes' if is_fix_bead(child) else ''} | "
                f"{_cell((child.metadata or {}).get('divergence') or '')} |"
            )
    else:
        out.append("_no children — this goal was closed as a single bead._")
    out.append("")

    out.extend(["## Divergence", ""])
    if diverged:
        for child in diverged:
            tag = str((child.metadata or {}).get("divergence"))
            out.append(f"- **{tag.upper()}** — `{child.id}` {_cell(child.title)}")
    else:
        out.append("_none tagged._")
    out.append("")
    out.append(DIVERGENCE_FOOTER)
    return "\n".join(out)


def write_goal_review(
    store_path: Path | str,
    goal: Task,
    all_tasks: list[Task],
    now: datetime | None = None,
) -> Path | None:
    """Write `<store-dir>/reviews/<goal-id>.md`. Never raises — warns instead.

    Returns the path written, or None when generation failed. The caller is a
    completion path: it must be able to ignore this entirely.
    """
    now = now or datetime.now(timezone.utc)
    try:
        children = [t for t in all_tasks if t.parent_id == goal.id]
        path = review_path(store_path, goal.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_review(goal, children, now))
        return path
    except Exception as e:  # noqa: BLE001 — paperwork must never fail the work
        print(
            f"[review] WARNING: could not write the system review for {goal.id} "
            f"({e}) — the goal is still DONE; only its review is missing",
            file=sys.stderr,
        )
        return None
