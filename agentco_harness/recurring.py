"""Recurring task definitions and the reconciliation generator.

Recurring tasks are a *generator*, not a bead type: beads stay dumb,
immutable, one-shot. All recurrence logic lives in `recurring.jsonl`
(beside the task queue) and is processed here at the top of every
heartbeat cycle.

Scheduling is reconciliation-based, never tick-based: we ask "given
`schedule` and `last_spawned`, is a run overdue?" — downtime causes
catch-up, not skips.

DETECTION IS LEVEL-TRIGGERED; ACTUATION IS ONE-SHOT. Noticing a definition
is due is free and may happen any number of times per period, from any
number of cycles. FIRING it is gated on winning a reservation keyed
`UNIQUE(schedule_id, period)` in `schedules.jsonl`, so a period fires at
most once no matter how many observers agree it is due.

That reservation replaces the module's original at-least-once contract
("the bead is fsynced before `last_spawned` moves, so a crash duplicates
rather than skips") with **at-most-once per period**. The trade was made
deliberately: the duplicate that at-least-once tolerates is cheap only
while the effect is a bead, and it is not — a schedule whose effect is a
message, a deploy or a spend has nothing to dedup after the fact, and one
finding fanning out to 95 beads is what the tolerant version cost. A
period lost to a crash between reserving and spawning is recovered by the
next period, and is visible in the audit as an observation gap; a
duplicate spend is not recoverable at all.

Every actual firing writes an observation, INCLUDING firings that produced
nothing, because `produced: 0` and "never fired" are different facts and
conflating them is what made F5 (a valid schedule with zero runs for ten
days) invisible. `agentco schedules audit` is the query that reads them.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import schedules
from .beads import Beads, Task, TaskStatus

_DURATION_RE = re.compile(r"^(\d+)\s*([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Statuses that count as "still open" for the dedup guard — a slow task
# that outlives its interval must not pile up duplicates behind it.
_OPEN_STATUSES = {TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED}


def parse_duration(spec: str) -> timedelta:
    """Parse an interval duration string (`15m`, `1h`, `1d`, `7d`).

    Interval schedules only — cron/calendar expressions are out of scope.
    Raises ValueError on anything unparseable or non-positive.
    """
    m = _DURATION_RE.match(str(spec).strip())
    if not m:
        raise ValueError(
            f"unparseable duration {spec!r} — expected <N><unit> with unit one of s/m/h/d"
        )
    value, unit = int(m.group(1)), m.group(2)
    if value <= 0:
        raise ValueError(f"duration must be positive, got {spec!r}")
    return timedelta(seconds=value * _UNIT_SECONDS[unit])


@dataclass
class RecurringDef:
    """One recurring task definition (one line in recurring.jsonl)."""

    id: str
    title: str
    schedule: dict
    agent: str | None = None
    payload: dict = field(default_factory=dict)
    last_spawned: str | None = None
    #: When this definition was declared. Stamped by `Recurring.add` when
    #: absent. Rows written before this field existed carry None, which readers
    #: must treat as "declared before any window they are looking at" — the
    #: only safe reading, and the true one for every historical row.
    #: `schedules.audit` uses it so a schedule declared an hour ago is not
    #: reported as having missed a fortnight of firings.
    created_at: str | None = None
    enabled: bool = True
    catch_up: str = "latest"  # "latest" | "all"
    budget: dict | None = None  # {"timeout": s, "max_turns": n} for claude executor

    def every(self) -> timedelta:
        return parse_duration(self.schedule["every"])

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "RecurringDef":
        """Deserialize. Unknown fields are ignored (forward compatibility);
        a missing/invalid schedule or catch_up raises ValueError — callers
        quarantine such lines rather than dropping them.
        """
        d = json.loads(line)
        known = {f.name for f in fields(cls)}
        d = {k: v for k, v in d.items() if k in known}
        obj = cls(**d)
        if not isinstance(obj.schedule, dict) or "every" not in obj.schedule:
            raise ValueError("schedule must be an object with an 'every' duration")
        parse_duration(obj.schedule["every"])  # validate at read so bad defs quarantine
        if obj.catch_up not in ("latest", "all"):
            raise ValueError(f"catch_up must be 'latest' or 'all', got {obj.catch_up!r}")
        return obj


class Recurring:
    """JSONL store of recurring definitions.

    Same contract as Beads: validation at write boundaries, tolerance at
    read boundaries — malformed lines are quarantined verbatim with a loud
    warning, never dropped.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._quarantined: list[str] = []
        self._warned_lines: set[str] = set()

    @contextmanager
    def _locked(self):
        with open(self._lock_path, "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _read_all(self) -> list[RecurringDef]:
        defs = []
        self._quarantined = []
        with open(self.path) as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    defs.append(RecurringDef.from_json(line))
                except (ValueError, KeyError, TypeError) as e:
                    self._quarantined.append(line)
                    if line not in self._warned_lines:
                        self._warned_lines.add(line)
                        print(
                            f"[recurring] WARNING: quarantined unparseable definition at "
                            f"{self.path}:{lineno} ({e}) — preserved, not scheduled"
                        )
        return defs

    def _write_all(self, defs: list[RecurringDef]) -> None:
        """Atomically rewrite, preserving quarantined raw lines.

        Temp file in the SAME directory + flush + fsync + os.replace so a crash
        mid-write can never truncate or corrupt the live store: the rename is
        atomic, leaving either the whole old file or the whole new one on disk.
        """
        directory = self.path.parent
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".recurring-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                for d in defs:
                    f.write(d.to_json() + "\n")
                for raw in self._quarantined:
                    f.write(raw + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def add(self, definition: RecurringDef) -> RecurringDef:
        """Append a definition. Validates schedule loudly at the write boundary."""
        parse_duration(definition.schedule["every"])
        if definition.created_at is None:
            definition.created_at = datetime.now(timezone.utc).isoformat()
        with self._locked():
            if any(d.id == definition.id for d in self._read_all()):
                raise ValueError(f"recurring definition id {definition.id!r} already exists")
            with open(self.path, "a") as f:
                f.write(definition.to_json() + "\n")
        return definition

    def list(self) -> list[RecurringDef]:
        return self._read_all()

    def get(self, def_id: str) -> RecurringDef | None:
        for d in self._read_all():
            if d.id == def_id:
                return d
        return None

    def update(self, def_id: str, **kwargs) -> RecurringDef | None:
        with self._locked():
            defs = self._read_all()
            for i, d in enumerate(defs):
                if d.id == def_id:
                    for key, value in kwargs.items():
                        if hasattr(d, key):
                            setattr(d, key, value)
                    defs[i] = d
                    self._write_all(defs)
                    return d
        return None


def _fsync_file(path: Path) -> None:
    """Force a file's contents to durable storage."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def any_due(recurring: Recurring, now: datetime | None = None) -> bool:
    """Read-only: is any enabled recurring def overdue for a spawn right now?

    Used by the adaptive-backoff gate as a reset signal — a due def means the
    cheap wake should run a full cycle (which then reconciles and spawns) rather
    than skip. Deliberately ignores the open-bead dedup guard: 'is it due' is a
    property of the schedule alone; whether it will actually spawn is
    `reconcile`'s job. No side effects, no writes.
    """
    now = now or datetime.now(timezone.utc)
    for d in recurring.list():
        if not d.enabled:
            continue
        every = d.every()
        if d.last_spawned is None:
            return True
        last = datetime.fromisoformat(d.last_spawned)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if int((now - last) / every) >= 1:
            return True
    return False


def reconcile(
    recurring: Recurring,
    beads: Beads,
    now: datetime | None = None,
) -> list[Task]:
    """Spawn beads for every overdue recurring definition.

    Runs at the top of every heartbeat cycle, before the orchestrator
    drains beads. Write order is bead → fsync → update last_spawned, so a
    crash in the window duplicates rather than skips.
    """
    now = now or datetime.now(timezone.utc)
    spawned: list[Task] = []

    open_by_def: dict[str, int] = {}
    for t in beads._read_all():
        src = t.metadata.get("spawned_by")
        if src and t.status in _OPEN_STATUSES:
            open_by_def[src] = open_by_def.get(src, 0) + 1

    for d in recurring.list():
        if not d.enabled:
            continue

        every = d.every()
        last = datetime.fromisoformat(d.last_spawned) if d.last_spawned else None
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)

        if last is None:
            missed = 1
        else:
            missed = int((now - last) / every)
        if missed < 1:
            continue

        # Dedup guard: a previous spawn is still open — do not pile up.
        # last_spawned is deliberately NOT advanced here: if the open bead
        # came from a crash window, the next cycle after it closes will
        # spawn again (duplicate, never skip).
        if open_by_def.get(d.id):
            print(
                f"[recurring] {d.id} ({d.title}): overdue but a spawned bead is "
                f"still open — skipping spawn to avoid pile-up"
            )
            continue

        if missed > 1:
            print(
                f"[recurring] WARNING: {d.id} ({d.title}) missed {missed} interval(s) "
                f"— spawning per catch_up={d.catch_up}"
            )

        count = missed if d.catch_up == "all" else 1
        for i in range(count):
            metadata = dict(d.payload)
            metadata["spawned_by"] = d.id
            if d.budget:
                metadata["budget"] = d.budget
            # The SLOT this spawn is for, on the schedule grid — the same
            # arithmetic `new_last` uses below, so the key names the interval
            # the bead is owed to rather than the wall-clock instant reconcile
            # happened to run at. `source_id` carries `now`, which is unique by
            # construction and therefore idempotent against nothing.
            slot = (last + (i + 1) * every) if last is not None else now
            period = slot.isoformat()

            # RESERVATION — the at-most-once gate, taken BEFORE the spawn.
            #
            # `last_spawned` is a cursor and cursors are not a mutual-exclusion
            # mechanism: two cycles racing (the hourly heartbeat plus a manual
            # `agentco cycle`, or a launchd job on each of two machines against a
            # synced store) can both read the same `last_spawned`, both compute
            # the same slot as due, and both fire it. The bead-level natural key
            # catches the duplicate BEAD, but only a bead — a schedule whose
            # effect is a message, a deploy or a spend has no bead to dedup.
            # UNIQUE(schedule_id, period) makes the FIRING itself one-shot, which
            # is what generalises.
            #
            # Losing the race is normal, not an error: somebody already fired
            # this period, so there is nothing left to do for it.
            if schedules.reserve(
                beads.path, d.id, period, now=now, detail=d.title
            ) is None:
                print(
                    f"[recurring] {d.id}: period {period} is already reserved — "
                    f"not firing twice"
                )
                continue

            task = beads.create(
                title=d.title,
                description=d.payload.get("description", d.title),
                assigned_agent=d.agent,
                source="recurring",
                source_id=f"{d.id}:{now.isoformat()}:{i}",
                metadata=metadata,
                # Generated work, keyed per (schedule, interval). In the happy
                # path this never fires: `last_spawned` advances phase-
                # preservingly, so no slot is ever offered twice. It fires in
                # the crash window the comment above acknowledges — create()
                # succeeded, the `last_spawned` write did not — which is the one
                # case the open-bead guard cannot catch, because by the next
                # cycle the duplicate's twin may already be closed.
                natural_key_kind="recurring",
                natural_key_subject=d.id,
                natural_key_period=slot.isoformat(),
            )
            if getattr(task, "natural_key_conflict", False):
                print(
                    f"[recurring] {d.id}: slot {slot.isoformat()} was already "
                    f"spawned as {task.id} — not spawning a duplicate"
                )
                # OBSERVED, produced NOTHING. Recorded, because `produced: 0` is
                # not the same fact as "never fired" and v1 could not tell them
                # apart — which is exactly why F5 was invisible for ten days.
                schedules.observe(
                    beads.path,
                    d.id,
                    period,
                    produced=0,
                    now=now,
                    detail="slot already spawned — no new bead",
                )
                continue
            spawned.append(task)
            schedules.observe(
                beads.path, d.id, period, produced=1, bead_ids=[task.id], now=now
            )
            print(f"[recurring] {d.id} spawned bead {task.id} ({d.title})")

        _fsync_file(beads.path)
        # Phase-preserving advance: catch-up lands last_spawned on the
        # schedule grid, not on wall-clock now, so intervals stay aligned.
        new_last = (last + missed * every) if last is not None else now
        recurring.update(d.id, last_spawned=new_last.isoformat())
        _fsync_file(recurring.path)

    return spawned


def supersede_resolved_rcas(beads: Beads) -> list[Task]:
    """Close root-cause analyses whose subject failure is no longer failing.

    An RCA bead is a *diagnosis of another bead* (``metadata.rca_for``). It exists to
    explain one failure, so it stops being actionable the moment that failure is no
    longer failing — whether it was fixed, superseded by a later success, or closed by
    hand. Nothing did that, so RCAs outlived their subjects and became the visible top
    of the queue once the sample failures beneath them were swept.

    Deliberately narrow: only an RCA whose subject EXISTS and is resolved is closed.
    An RCA pointing at a bead that is still failing is live, and one pointing at a
    missing ID is left alone — a dangling reference is a bug to surface, not to hide.
    """
    tasks = beads._read_all()
    by_id = {t.id: t for t in tasks}
    closed: list[Task] = []
    for t in tasks:
        if t.status != TaskStatus.FAILED or t.source != "rca":
            continue
        subject_id = (t.metadata or {}).get("rca_for")
        subject = by_id.get(subject_id) if subject_id else None
        if subject is None or subject.status == TaskStatus.FAILED:
            continue
        # Bypasses the RCA terminal-action gate deliberately. That gate exists
        # to stop an analysis from closing with no fix behind it while the
        # failure is still live; here the failure is NOT live — its subject is
        # resolved — so demanding a fix bead for a failure that no longer
        # exists would strand every superseded RCA in verify_failed forever.
        # This is code deciding on store-visible evidence, not an executor
        # grading its own work.
        done = beads.update(
            t.id,
            status=TaskStatus.DONE,
            result=json.dumps(
                {
                    "status": "complete",
                    "output": (
                        f"Superseded: the failure this RCA diagnoses ({subject_id}) is "
                        f"now {subject.status.value}. Original error: "
                        f"{(t.metadata or {}).get('rca_error', 'not recorded')}"
                    ),
                }
            ),
            verify_gate=False,
        )
        if done is not None:
            closed.append(done)
    if closed:
        _fsync_file(beads.path)
    return closed


def _sampler_family(t: Task) -> str | None:
    """The periodic sampler a bead is an instance of, or None if it is real work.

    Two mechanisms spawn samples on a schedule and they key differently: recurring
    definitions stamp ``metadata.spawned_by``, feed ingestion stamps
    ``metadata.feed_source_id``. Both produce a series where a failure is only news
    until the next sample succeeds. Anything else — a task a human or an agent filed —
    is NOT a sample, and its failure must never be swept.
    """
    md = t.metadata or {}
    if t.source == "recurring" and md.get("spawned_by"):
        return f"recurring:{md['spawned_by']}"
    if t.source == "feeds" and md.get("feed_source_id"):
        return f"feeds:{md['feed_source_id']}:{md.get('feed_kind', '')}"
    return None


def supersede_stale_failures(beads: Beads) -> list[Task]:
    """Close periodic-sampler failures that a later run of the same source disproved.

    A recurring bead is a *sample*, not a task. When `verify-sommeliwhey` fails at
    14:00 and passes at 15:00, the 14:00 failure stopped being actionable the moment
    15:00 came back green — but nothing ever closed it. `_OPEN_STATUSES` deliberately
    excludes FAILED so a failure does not block the next run (correct), and the
    consequence is that every failure lives forever.

    The damage compounds: 425 `Verify child instance: sommeliwhey` beads existed on
    2026-08-06, 388 of them done and 37 failed — and all 37 sorted to the top of the
    queue as "needs you". A queue that is never empty carries no signal, so the real
    cost is not the rows, it is that the operator stops reading the surface at all.

    Only failures STRICTLY OLDER than a later success are superseded. The most recent
    sample is never touched — if the last run failed, that is live news and it stays.
    """
    by_def: dict[str, list[Task]] = {}
    for t in beads._read_all():
        family = _sampler_family(t)
        if family:
            by_def.setdefault(family, []).append(t)

    superseded: list[Task] = []
    for def_id, tasks in by_def.items():
        newest_ok = max(
            (t.created_at for t in tasks if t.status == TaskStatus.DONE),
            default=None,
        )
        if newest_ok is None:
            continue  # never passed — every failure is still live news
        for t in tasks:
            if t.status != TaskStatus.FAILED or t.created_at >= newest_ok:
                continue
            closed = beads.complete(
                t.id,
                result=json.dumps(
                    {
                        "status": "complete",
                        "output": (
                            f"Superseded: {def_id} passed at {newest_ok}, after this "
                            f"sample failed at {t.created_at}. A health-check failure "
                            f"is news only until the next check clears it."
                        ),
                    }
                ),
            )
            if closed is not None:
                superseded.append(closed)
    if superseded:
        _fsync_file(beads.path)
    return superseded
