"""agentco me — the human work queue.

Companies run in parallel, each with its own local priorities; the one
single-threaded resource in the portfolio is the human. This module builds
the ONE list that matters to them: everything that depends on a person,
collected read-only across this instance and every registered child
(recursively), ranked by company weight x severity x age plus unblock
leverage — how much machine work is waiting behind the decision.

Pure code, no LLM. The list is a view recomputed from ground truth on every
call, never a document anyone maintains.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .beads import Beads, Task, TaskResult, TaskStatus
from .children import ChildRegistry, verify_child
from .config import Config
from .tempo import Schedule, explain, is_pin, schedule, temporal_score

# Lower company priority number = more important (mirrors TaskPriority).
_COMPANY_WEIGHT = {0: 4.0, 1: 3.0, 2: 2.0, 3: 1.0}

_SEVERITY = {
    "stale_child": 5.0,  # a dead company blocks its entire queue
    "failed": 4.0,
    # A gate that RAN and said no is a hard, evidenced failure — a hair below a
    # crashed bead only because the state is understood and the output is on the
    # bead. It is not "someday" work: everything sequenced behind it is stuck.
    "verify_failed": 3.8,
    "human_assigned": 3.5,  # a person owns it — between failed and needs_input
    "needs_input": 3.0,
    # The work is finished and only a person's yes stands between it and the
    # whole downstream chain — above a plain proposal approval, which gates
    # work that has not been done yet.
    "verify_gate": 2.8,
    "approval": 2.5,
    "blocked": 2.0,
}

# needs_input results older than this are assumed handled out of band.
_NEEDS_INPUT_WINDOW_SECONDS = 14 * 86400

_LEVERAGE_BONUS = 0.5  # score points per task waiting behind this item

# How hard a deadline may bend the standing ranking. Applied MULTIPLICATIVELY
# so company weight and severity keep their meaning: a dead child blocking a
# whole queue still outranks one late deliverable. A task with no temporal data
# scores 0.0 in tempo, giving a multiplier of exactly 1.0 — so a queue that has
# never seen a due date ranks precisely as it did before this existed.
_TEMPO_GAIN = 2.0

# Beads priority is 0..3, CRITICAL..LOW. Tempo's importance term wants 0..1
# with 1 = most important. Mapped, never duplicated: a queue with two priority
# fields inflates both, which is the one thing the ranking must not do.
_IMPORTANCE_FROM_PRIORITY = {0: 1.0, 1: 0.7, 2: 0.4, 3: 0.15}

# A bead's own priority as a multiplier on standing rank. MEDIUM is exactly 1.0 so
# the default-priority queue is unchanged; only beads explicitly marked move.
_TASK_URGENCY = {0: 1.6, 1: 1.25, 2: 1.0, 3: 0.8}


@dataclass
class MeItem:
    """One thing that depends on a human, portfolio-wide."""

    company: str
    company_priority: int
    kind: str  # stale_child | failed | needs_input | approval | blocked
    task_id: str | None
    title: str
    detail: str
    age_seconds: float
    leverage: int
    config_path: str
    resolve: str
    priority: int = 2  # the bead's own TaskPriority; 2 = MEDIUM = no effect
    score: float = field(default=0.0)
    # --- temporal (tempo.py); all None/0 when the bead carries no time data ---
    slack_hours: float | None = None  # (effective due - now) - work remaining
    latest_start: str | None = None  # ISO point of no return
    temporal: float = 0.0  # 0..1 urgency; 0 = no deadline anywhere downstream
    why: str = ""  # one sentence, the dominant driver

    def to_dict(self) -> dict:
        return asdict(self)


def _weight(priority: int) -> float:
    return _COMPANY_WEIGHT.get(priority, 2.0)


def _urgency(task_priority: int) -> float:
    """How much a bead's OWN priority bends the standing ranking.

    Before this existed, `task.priority` reached the score only through
    `temporal_score(importance=...)` — and `temporal` is 0 for any bead with no
    deadline anywhere downstream, which makes the multiplier exactly 1.0. So on an
    undated queue, marking a bead CRITICAL changed nothing at all. the operator hit this
    directly on 2026-08-06: "if this is my highest priority, they should show up
    first" — they did not, and could not.

    Modest on purpose. CRITICAL lifts by 60%, so a critical bead in a default-weight
    company (2.0 x 1.6 = 3.2) still ranks below a top-priority company's ordinary work
    (4.0). Company weight stays the dominant axis; this breaks the "everything in one
    company looks identical" tie that made the queue unreadable.
    """
    return _TASK_URGENCY.get(task_priority, 1.0)


def _age_factor(seconds: float) -> float:
    # Grows linearly for a week, then saturates — old items rise but never
    # drown a critical company's fresh failure.
    return 1.0 + min(max(seconds, 0.0) / 86400.0, 7.0) * 0.25


def _score(item: MeItem) -> float:
    base = _weight(item.company_priority) * _SEVERITY[item.kind] * _urgency(item.priority)
    standing = base * _age_factor(item.age_seconds) + _LEVERAGE_BONUS * item.leverage
    # Multiplier is exactly 1.0 when item.temporal == 0.0 (no deadline), so the
    # pre-tempo ranking is preserved bit-for-bit for untimed queues.
    return standing * (1.0 + _TEMPO_GAIN * item.temporal)


def _age_of(task: Task, now: datetime) -> float:
    try:
        created = datetime.fromisoformat(task.created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return (now - created).total_seconds()
    except (ValueError, TypeError):
        return 0.0


def _leverage_of(task_id: str, open_tasks: list[Task]) -> int:
    return sum(1 for t in open_tasks if task_id in t.blocked_by)


def _is_human_assigned(task: Task) -> bool:
    return isinstance(task.assigned_to, str) and task.assigned_to.startswith("human:")


def _is_snoozed(task: Task, now: datetime) -> bool:
    """True while a task's metadata `snoozed_until` timestamp is in the future.

    Read-boundary tolerance: an unparseable snoozed_until is treated as not
    snoozed (the item stays visible) — a corrupt timestamp must never hide a
    human's work forever.
    """
    stamp = task.metadata.get("snoozed_until")
    if not stamp:
        return False
    try:
        until = datetime.fromisoformat(str(stamp))
    except (ValueError, TypeError):
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return now < until


def _resolve_cmd(config_path: str, verb: str, task_id: str | None = None) -> str:
    tail = f" {task_id}" if task_id else ""
    return f"agentco --config {config_path} {verb}{tail}"


def _collect_instance(
    config: Config, config_path: str, company: str, priority: int, now: datetime
) -> list[MeItem]:
    """Everything human-gated in ONE instance's own queue. Read-only."""
    beads = Beads(config.tasks_path)
    all_tasks = beads.list()
    done_ids = {t.id for t in all_tasks if t.status == TaskStatus.DONE}
    open_tasks = [
        t
        for t in all_tasks
        if t.status not in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.SKIPPED)
    ]
    items: list[MeItem] = []

    # One backward pass for the whole instance, O(V+E). Deadlines propagate
    # here, so a blocker inherits the tightest deadline of anything it feeds —
    # graph leverage enters through the deadline itself, not as a side term.
    schedules: dict[str, Schedule] = schedule(all_tasks, now=now)

    def add(kind: str, task: Task, detail: str, verb: str) -> None:
        sched = schedules.get(task.id)
        leverage = _leverage_of(task.id, open_tasks)
        age = _age_of(task, now)
        items.append(
            MeItem(
                company=company,
                company_priority=priority,
                kind=kind,
                task_id=task.id,
                title=task.title,
                detail=detail,
                age_seconds=age,
                leverage=leverage,
                config_path=config_path,
                resolve=_resolve_cmd(config_path, verb, task.id),
                slack_hours=(
                    round(sched.slack_hours, 2)
                    if sched and sched.slack_hours is not None
                    else None
                ),
                latest_start=(
                    sched.latest_start.isoformat()
                    if sched and sched.latest_start
                    else None
                ),
                temporal=round(
                    temporal_score(
                        task,
                        sched,
                        importance=_IMPORTANCE_FROM_PRIORITY.get(task.priority.value, 0.4),
                        leverage=leverage,
                        age_seconds=age,
                    ),
                    4,
                ),
                why=explain(task, sched),
                priority=task.priority.value,
            )
        )

    for t in all_tasks:
        # Snooze applies to EVERY kind of item, not just human-assigned ones:
        # `me` promises "hide it until the interval elapses", so the check lives
        # here at the top of the loop, before any branch emits. A human item that
        # is failed or needs-input flows through the non-human branches below, and
        # snoozing it must hide it there too — a per-branch check would leave
        # those silently un-snoozable.
        if _is_snoozed(t, now):
            continue

        # Verify states come FIRST, ahead of the human-assigned branch: a gated
        # bead may also carry assigned_to, and the action a person must take at
        # a gate ("approve or reject this claim") is not the action for an open
        # assignment ("do the work"). Routing it as human_assigned would offer
        # `tasks complete` — the one command the gate exists to intercept.
        if t.status == TaskStatus.AWAITING_VERIFY:
            spec = t.metadata.get("verify") or {}
            add(
                "verify_gate",
                t,
                f"claimed done — awaiting your approval: {str(spec.get('check', ''))[:100]}",
                "tasks approve-verify",
            )
            continue
        if t.status == TaskStatus.VERIFY_FAILED:
            record = t.metadata.get("verify_result") or {}
            detail = str(record.get("output_tail", ""))[:120] or "verify gate failed"
            add("verify_failed", t, detail, "tasks show")
            continue

        # A person owns this task — it is the reason `me` became a bidirectional
        # work surface. Emit it explicitly (the other branches key on status
        # only, so without this a human-assigned task would be invisible in the
        # one list meant to hold it). Any OPEN task (pending/blocked/…) counts.
        if _is_human_assigned(t) and t.status not in (
            TaskStatus.DONE,
            TaskStatus.FAILED,
            TaskStatus.SKIPPED,
        ):
            # PIN beads (calendar events) are deliberately NOT me-items: a
            # meeting is not a decision to make or work to rank — reminding is
            # a notifier's job. They exist as beads so tempo's feasibility can
            # subtract their hours; surfacing every meeting here would bury
            # the actual decisions under schedule noise.
            if is_pin(t):
                continue
            name = t.assigned_to.split(":", 1)[1] or t.assigned_to
            add("human_assigned", t, f"assigned to {name}", "tasks complete")
            continue
        if t.status == TaskStatus.PENDING_APPROVAL:
            add("approval", t, "agent-proposed, waiting for approval", "approve task")
        elif t.status == TaskStatus.FAILED:
            err = str(t.metadata.get("error", ""))[:120] or "failed, no error recorded"
            add("failed", t, err, "tasks retry")
        elif t.status == TaskStatus.BLOCKED:
            add("blocked", t, "explicitly blocked", "tasks show")
        elif t.status == TaskStatus.PENDING and any(
            b not in done_ids for b in t.blocked_by
        ):
            waiting_on = [b for b in t.blocked_by if b not in done_ids]
            add("blocked", t, f"waiting on {', '.join(waiting_on)}", "tasks show")
        elif t.status == TaskStatus.DONE:
            tr = TaskResult.from_task(t)
            if (
                tr is not None
                and tr.status == "needs_input"
                and _age_of(t, now) <= _NEEDS_INPUT_WINDOW_SECONDS
            ):
                add("needs_input", t, (tr.continuation_hint or tr.output or "")[:120], "tasks show")
    return items


def collect(
    config_path: str,
    company: str | None = None,
    priority: int = 2,
    now: datetime | None = None,
    _seen: set[str] | None = None,
) -> list[MeItem]:
    """Walk this instance and every registered child (recursively), read-only.

    An unreadable child is reported as a stale_child item rather than raised —
    a company whose config is broken is, by definition, something that
    depends on the human.
    """
    now = now or datetime.now(timezone.utc)
    seen = _seen if _seen is not None else set()

    real = str(Path(config_path).resolve())
    if real in seen:
        return []  # registry cycle — already walked
    seen.add(real)

    config = Config.load(config_path)
    name = company or config.instance_name
    items = _collect_instance(config, config_path, name, priority, now)

    registry = ChildRegistry(config.children_registry_path)
    for child in registry.list():
        # A pathless child (ado-backed, vault-only, manual) has no local
        # config.yaml to walk — Path(None) would crash. verify_child reports
        # such children as 'unverified' (ok), so there is nothing local to do.
        #
        # A REMOTE child (ac-39d4dbc8) has a path, but it names a directory on
        # ANOTHER machine. Opening it here is not merely useless — a same-named
        # directory that happens to exist locally would be read as that node's
        # real queue. The registry's founding assumption was that every child is
        # locally mountable; the MacBook node is the first one that is not, so
        # the walk is skipped by declaration rather than by whether a stat()
        # happens to succeed.
        local = child.path and not child.is_remote
        child_config = Path(child.path) / "config.yaml" if local else None
        child_priority = getattr(child, "priority", 2)

        health = verify_child(child, now=now)
        # 'unverified' is expected for ado-backed/manual children and 'remote'
        # for off-machine nodes — neither is a problem. Only warn/fail/stale
        # states are something the human owns.
        if health["level"] not in ("ok", "unverified", "remote"):
            # Leverage = the child's whole open queue is stuck behind this.
            stuck = 0
            if child_config and child_config.is_file():
                try:
                    child_beads = Beads(Config.load(str(child_config)).tasks_path)
                    stuck = len(child_beads.ready()) + len(
                        child_beads.list(status=TaskStatus.IN_PROGRESS)
                    )
                except Exception:  # noqa: BLE001 — health item still reported
                    pass
            staleness = health.get("staleness_seconds") or 0.0
            items.append(
                MeItem(
                    company=child.name,
                    company_priority=child_priority,
                    kind="stale_child",
                    task_id=None,
                    title=f"child '{child.name}' is {health['level']}",
                    detail=health["detail"],
                    age_seconds=float(staleness),
                    leverage=stuck,
                    config_path=str(child_config) if child_config else "",
                    resolve=(
                        _resolve_cmd(str(child_config), "doctor")
                        if child_config
                        else ""
                    ),
                )
            )

        if child_config and child_config.is_file():
            items.extend(
                collect(
                    str(child_config),
                    company=child.name,
                    priority=child_priority,
                    now=now,
                    _seen=seen,
                )
            )
        elif local:
            # Deliberately NOT warned for a remote child: its queue being
            # invisible from here is the design, not a fault, and a warning
            # every single run is how a real signal gets tuned out.
            print(
                f"[me] WARNING: child '{child.name}' has no config.yaml at "
                f"{child.path} — its queue is invisible to this list",
                file=sys.stderr,
            )

    return items


def ranked(config_path: str, now: datetime | None = None) -> list[MeItem]:
    """The human work queue, highest score first."""
    items = collect(config_path, now=now)
    for item in items:
        item.score = round(_score(item), 2)
    return sorted(items, key=lambda i: -i.score)


def portfolio_tasks(
    config_path: str, _seen: set[str] | None = None
) -> list[Task]:
    """Every bead across this instance and all registered children, read-only.

    The graph is per-company but the time resource is one person: a Sommeli
    deadline and an M3BL deadline compete for the same Tuesday afternoon, so
    feasibility computed per-node systematically understates the load. This is
    the collector that lets tempo answer at the only level where the answer is
    true — the portfolio.

    Same walk discipline as ``collect``: registry cycles are cut with a seen
    set, an unreadable child is skipped with a warning rather than raised (its
    absence makes the feasibility check OPTIMISTIC, which is the direction a
    skipped input must err — never inventing load, only missing it), and
    nothing is ever written.
    """
    seen = _seen if _seen is not None else set()
    real = str(Path(config_path).resolve())
    if real in seen:
        return []
    seen.add(real)

    try:
        config = Config.load(config_path)
        tasks = list(Beads(config.tasks_path).list())
    except Exception as e:  # noqa: BLE001 — one broken node must not hide the rest
        print(
            f"[tempo] WARNING: could not read {config_path} ({e}) — its beads "
            f"are missing from the feasibility check, which is now optimistic",
            file=sys.stderr,
        )
        return []

    registry = ChildRegistry(config.children_registry_path)
    for child in registry.list():
        # Remote children are skipped for the same reason as in `collect`: their
        # path names another machine's disk. The omission keeps this check
        # optimistic (missing load, never inventing it) — the direction a
        # skipped input must err, per this function's contract above.
        if child.is_remote or not child.path:
            continue
        child_config = Path(child.path) / "config.yaml"
        if child_config.is_file():
            tasks.extend(portfolio_tasks(str(child_config), _seen=seen))
    return tasks
