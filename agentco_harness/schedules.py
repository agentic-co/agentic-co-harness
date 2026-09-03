"""Schedules: a registry, a per-period reservation, and an expected-vs-observed audit.

WHY THIS EXISTS
---------------
F5, verbatim from the corpus: a recurring definition existed with a valid
schedule and had **zero runs for 10+ days**. The work it described did happen —
via a launchd plist and by hand — so nothing downstream looked broken, and
nothing noticed. Silent non-execution is the worst failure class this system
produces because it is indistinguishable, from every surface, from "nothing
needed doing".

The reason nothing noticed is that v1 recorded *intent* (``recurring.jsonl``)
and recorded *effects* (beads), but never recorded **firings**. ``last_spawned``
is a cursor, not evidence: it moves when a spawn happens and is silent about
every period in which one did not, and it cannot distinguish "fired and produced
nothing" from "never fired". So there was no query that could be asked. This
module adds the missing noun.

THE TWO HALVES, AND WHY THEY ARE THE SAME CHANGE
------------------------------------------------
The corpus's two most expensive incident families are opposite failures of one
missing distinction:

* **Silent non-execution** (F5) — detection that never ran.
* **Duplicate fan-out** (M12: one finding became 95 beads, a node-day at 100%
  duplicate spend) — actuation that ran too often.

A naive fix for the first causes the second: "notice a schedule is due and fire
it" run by several cycles, or re-run after a crash, is a schedule storm. So
detection here is level-triggered and free (recompute due-ness every cycle from
the world), while **actuation is one-shot and gated**: a firing must first win a
``Reservation`` row keyed ``UNIQUE(schedule_id, period)``. Any number of cycles
may observe the same period as due; exactly one can fire it.

FORWARD COMPATIBILITY
---------------------
v2 models reservations as ``UNIQUE(kind, subject, epoch)`` and schedules as a
first-class ``Schedule`` resource. Rows written here carry ``kind`` /
``subject`` / ``period`` under exactly that reading, and the uniqueness key is
built by :mod:`agentco_harness.natural_key` — the same one derivation the ingest path
uses, so schedules do not become a seventh idempotency mechanism.

INDEPENDENTLY VALUABLE
----------------------
Nothing here needs v2. The audit answers "which schedules are silently not
firing?" against the live JSONL stores today, and derives historical
observations from the bead store so it has an answer on the day it ships rather
than after a fortnight of accumulation.

FORMAT: append-only JSONL beside the node's ``tasks.jsonl``, one object per
line, ``schema`` stamped — same doctrine as ``tasks.jsonl``, ``usage.jsonl``
and ``runs.jsonl``.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from .natural_key import generated_key

LEDGER_NAME = "schedules.jsonl"

#: Stamped on every row. Readers tolerate rows without it.
SCHEMA = "agentco_harness.schedules/1"

#: Row discriminator values.
ROW_RESERVATION = "reservation"
ROW_OBSERVATION = "observation"

#: The reservation namespace. v2's ``Reservation.kind``.
RESERVATION_KIND = "schedule"

#: How many consecutive expected-but-unobserved periods make a schedule a
#: finding rather than a blip. Three is the smallest number that cannot be one
#: late cycle plus one clock skew: F5 ran 10+ periods silent, and a marginal
#: single miss is the signature of a false positive, which spends alarm
#: credibility for nothing.
DEFAULT_MIN_SILENT_PERIODS = 3

#: Default audit window. Long enough that a daily schedule accumulates a
#: verdict, short enough that a schedule disabled last month is not re-litigated.
DEFAULT_WINDOW_DAYS = 14

# --- consequence class -------------------------------------------------------
# Every check declares a consequence class and consumers subscribe to a class.
# A hygiene finding must never be able to change a deployment consumer's input
# — that conflation took Recorro offline for 8h30m across 8 aborted cycles.
#
# A schedule that has stopped firing is a LIVENESS finding: the plane is alive
# and wrong, nothing is corrupt, nothing is unsafe, nothing is deployed.
# Backport 4 landed the canonical class -> exit-code table in `doctor`, so
# these two constants now live there and are imported here unchanged. This
# module keeps its OWN single-class exit code: `agentco schedules audit` reports
# exactly one class, so a caller gating on it can never be handed a code that
# means something else. `doctor` maps the same finding into its aggregate as
# BROKEN.
from .doctor import EXIT_LIVENESS, EXIT_OK  # noqa: F401  (re-exported)

CONSEQUENCE_CLASS = "liveness"


# --------------------------------------------------------------------------- #
# Paths and locking
# --------------------------------------------------------------------------- #

def ledger_path(tasks_path: str | Path) -> Path:
    """The schedule ledger lives beside the bead store whose work it fires."""
    return Path(tasks_path).expanduser().parent / LEDGER_NAME


@contextmanager
def _locked(path: Path):
    """Advisory lock on the ledger, held across read-then-append.

    The UNIQUE constraint is enforced by scanning under this lock. Without it,
    two cycles could both scan, both find the period free, and both append —
    which is precisely the storm the reservation exists to prevent. The lock
    is the ledger's own, never the bead store's, so no caller can deadlock by
    reserving while holding the store.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def read_ledger(tasks_path: str | Path) -> list[dict]:
    """Every well-formed row. A malformed line is skipped on READ, never
    removed from the file — quarantine, not deletion."""
    path = ledger_path(tasks_path)
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


# --------------------------------------------------------------------------- #
# Reservations — the UNIQUE(schedule_id, period) constraint
# --------------------------------------------------------------------------- #

def reservation_key(schedule_id: str, period: str) -> str:
    """``gen|schedule|<schedule_id>|<period>`` — the uniqueness key.

    Built by the same derivation the bead ingest path uses, so there is one
    uniqueness rule in this system rather than one per mechanism.
    """
    return generated_key(RESERVATION_KIND, schedule_id, period)


def reserve(
    tasks_path: str | Path,
    schedule_id: str,
    period: str,
    *,
    now: Optional[datetime] = None,
    detail: Optional[str] = None,
) -> Optional[dict]:
    """Win the right to fire ``schedule_id`` for ``period``, or return None.

    This is the whole idempotency story. Returning ``None`` is not an error —
    it is the normal, expected answer when another cycle (or an earlier pass of
    this one, or a retry after a crash) already fired that period. Callers
    treat it as "someone else has this" and move on.

    Deliberately taken BEFORE the work, not after: a reservation held by a
    firing that then crashed burns that period, which is at-most-once. The
    alternative — reserve after success — is at-least-once, and at-least-once
    on an actuator that spends money is how one finding became 95 beads.
    """
    path = ledger_path(tasks_path)
    key = reservation_key(schedule_id, period)
    at = (now or datetime.now(timezone.utc)).isoformat()
    with _locked(path):
        for row in read_ledger(tasks_path):
            if row.get("type") == ROW_RESERVATION and row.get("key") == key:
                return None
        row = {
            "schema": SCHEMA,
            "type": ROW_RESERVATION,
            "key": key,
            "kind": RESERVATION_KIND,
            "subject": schedule_id,
            "period": period,
            "at": at,
        }
        if detail:
            row["detail"] = detail
        _append(path, row)
        return row


def observe(
    tasks_path: str | Path,
    schedule_id: str,
    period: str,
    *,
    produced: int = 0,
    bead_ids: Optional[list[str]] = None,
    detail: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Record that ``schedule_id`` ACTUALLY FIRED for ``period``.

    ``produced`` is recorded separately from the fact of firing because
    ``produced: 0`` is not the same as "never fired", and v1 could not tell
    them apart. A schedule that fires nightly and produces nothing is healthy
    or broken depending on what it is for; a schedule that never fires is
    always broken. Conflating them is what made F5 invisible.

    Never raises on a write failure: evidence must not be able to fail the work
    it is evidence of.
    """
    path = ledger_path(tasks_path)
    row = {
        "schema": SCHEMA,
        "type": ROW_OBSERVATION,
        "key": reservation_key(schedule_id, period),
        "kind": RESERVATION_KIND,
        "subject": schedule_id,
        "period": period,
        "at": (now or datetime.now(timezone.utc)).isoformat(),
        "produced": int(produced),
        "bead_ids": list(bead_ids or []),
    }
    if detail:
        row["detail"] = detail
    try:
        _append(path, row)
    except Exception as e:  # noqa: BLE001 — telemetry is never load-bearing
        print(f"[schedules] WARNING: could not record firing of {schedule_id}: {e}")
    return row


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #

@dataclass
class Schedule:
    """One schedule the plane knows about.

    ``interval`` and ``cron`` are both carried because v2's ``Schedule``
    resource admits either; v1's generator only implements intervals, so
    ``cron`` is always ``None`` today and is here so a cron-backed schedule can
    be registered without a schema change.
    """

    id: str
    node: str
    interval: Optional[str]  # duration spec, e.g. "1d"
    cron: Optional[str]
    fires: str  # what this schedule produces, in words
    enabled: bool
    store: str  # the tasks.jsonl this schedule's firings land in
    #: When the definition was declared, if known. ``None`` on rows written
    #: before `RecurringDef.created_at` existed, and read as "older than any
    #: window" — true for every such row by construction.
    created_at: Optional[str] = None

    def every(self) -> Optional[timedelta]:
        if not self.interval:
            return None
        from .recurring import parse_duration

        try:
            return parse_duration(self.interval)
        except ValueError:
            return None

    def to_dict(self) -> dict:
        return asdict(self)


def node_of(tasks_path: str | Path) -> str:
    """The node a store belongs to, from where the store lives."""
    from .usage import node_name

    return node_name(tasks_path)


def registry(config) -> list[Schedule]:
    """Every schedule owned by ONE node, derived from its recurring store.

    Derived rather than a second file on purpose: a registry that must be kept
    in sync with ``recurring.jsonl`` by hand is a registry that will disagree
    with it, and then the audit is auditing the wrong list. The recurring store
    IS the definition; this is a typed view of it that the audit and v2 can both
    read.
    """
    from .recurring import Recurring

    node = node_of(config.tasks_path)
    out: list[Schedule] = []
    for d in Recurring(config.recurring_path).list():
        fires = d.title or d.id
        if d.agent:
            fires = f"{fires} → {d.agent}"
        out.append(
            Schedule(
                id=d.id,
                node=node,
                interval=str(d.schedule.get("every")) if isinstance(d.schedule, dict) else None,
                cron=None,
                fires=fires,
                enabled=bool(d.enabled),
                store=str(config.tasks_path),
                created_at=d.created_at,
            )
        )
    return out


def portfolio_registry(config_path: str, _seen: Optional[set[str]] = None) -> list[Schedule]:
    """Schedules across this node and every registered local child.

    Same walk discipline as ``me.portfolio_tasks``: registry cycles cut with a
    seen set, remote children skipped (their path names another machine's disk),
    an unreadable node warned about rather than raised. A skipped node makes the
    audit report FEWER schedules, never fabricated ones — the direction a missing
    input must err.
    """
    from .children import ChildRegistry
    from .config import Config

    seen = _seen if _seen is not None else set()
    real = str(Path(config_path).resolve())
    if real in seen:
        return []
    seen.add(real)

    try:
        config = Config.load(config_path)
        out = registry(config)
    except Exception as e:  # noqa: BLE001 — one broken node must not hide the rest
        print(
            f"[schedules] WARNING: could not read {config_path} ({e}) — its "
            f"schedules are missing from this audit",
            file=sys.stderr,
        )
        return []

    try:
        children = ChildRegistry(config.children_registry_path).list()
    except Exception:  # noqa: BLE001
        children = []
    for child in children:
        if child.is_remote or not child.path:
            continue
        child_config = Path(child.path) / "config.yaml"
        if child_config.is_file():
            out.extend(portfolio_registry(str(child_config), _seen=seen))
    return out


# --------------------------------------------------------------------------- #
# Observations, including the ones that predate this module
# --------------------------------------------------------------------------- #

def _parse_at(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def derived_observations(tasks_path: str | Path) -> list[dict]:
    """Firings reconstructed from the bead store, for history that predates
    this ledger.

    A recurring definition stamps ``metadata.spawned_by`` on every bead it
    spawns, so a bead is durable evidence that its schedule fired at
    ``created_at``. That is strictly weaker than an observation — it cannot see
    a firing that produced nothing — but it is the honest reading of what the
    store actually knows, and it means the audit has a real answer on day one
    instead of reporting every schedule silent because the ledger is new.

    Weaker in the safe direction: it can only make a schedule look MORE alive
    than it is, never less, so a schedule this function still reports as silent
    is silent by both readings.
    """
    from .beads import Beads

    path = Path(tasks_path)
    if not path.exists():
        return []
    rows: list[dict] = []
    for task in Beads(path)._read_all():
        if task.source != "recurring":
            continue
        sid = (task.metadata or {}).get("spawned_by")
        if not sid:
            continue
        rows.append(
            {
                "type": ROW_OBSERVATION,
                "subject": sid,
                "period": task.created_at,
                "at": task.created_at,
                "produced": 1,
                "bead_ids": [task.id],
                "derived": True,
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# The audit
# --------------------------------------------------------------------------- #

@dataclass
class ScheduleAudit:
    """Expected versus observed for one schedule, over one window."""

    schedule: Schedule
    window_start: str
    window_end: str
    expected: int
    observed: int
    produced: int
    last_observed_at: Optional[str]
    silent: bool
    reason: str = ""

    def to_dict(self) -> dict:
        d = {
            "id": self.schedule.id,
            "node": self.schedule.node,
            "interval": self.schedule.interval,
            "cron": self.schedule.cron,
            "fires": self.schedule.fires,
            "enabled": self.schedule.enabled,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "expected": self.expected,
            "observed": self.observed,
            "produced": self.produced,
            "last_observed_at": self.last_observed_at,
            "silent": self.silent,
            "consequence_class": CONSEQUENCE_CLASS,
        }
        if self.reason:
            d["reason"] = self.reason
        return d


def expected_firings(schedule: Schedule, window: timedelta) -> int:
    """How many times this schedule should have fired across ``window``.

    A schedule with no parseable interval expects 0 — it is unschedulable, and
    claiming it missed firings would be inventing a finding out of a parse
    failure. ``recurring`` already quarantines such definitions loudly at its
    own layer, which is where that failure belongs.

    ``window`` is the schedule's OWN exposure, not the audit window: a schedule
    declared an hour ago cannot have missed a fortnight of firings. Callers pass
    the clamped span; see ``_exposure``.
    """
    every = schedule.every()
    if every is None or every <= timedelta(0):
        return 0
    return int(window / every)


def _exposure(schedule: Schedule, start: datetime, now: datetime) -> timedelta:
    """How long this schedule has actually been declared inside the window.

    A definition added five minutes ago has been exposed for five minutes, so it
    expects zero firings and cannot be silent. Without this, every newly
    declared schedule is born red — which is how a check that fires on healthy
    systems teaches an operator to ignore it, the exact failure this whole
    consequence-class effort exists to reverse.

    A schedule with no ``created_at`` (every row written before the field
    existed) is treated as fully exposed. That is the safe direction: it keeps
    the F5 finding on the historical defs that motivated the audit, and those
    rows genuinely predate any window.
    """
    declared = _parse_at(schedule.created_at)
    if declared is None or declared <= start:
        return now - start
    if declared >= now:
        return timedelta(0)
    return now - declared


def audit(
    schedules: Iterable[Schedule],
    observations: Iterable[dict],
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_silent_periods: int = DEFAULT_MIN_SILENT_PERIODS,
    now: Optional[datetime] = None,
) -> list[ScheduleAudit]:
    """Expected versus observed, per schedule, over the trailing window.

    A **disabled** schedule is excluded outright rather than reported as
    healthy: it is not expected to fire, so counting it either way is noise, and
    noise in this report is what makes the report stop being read.

    A schedule is SILENT — the F5 finding — when it expected at least
    ``min_silent_periods`` firings in the window and produced **zero**
    observations. Not "fewer than expected": a schedule that fires late, or that
    the dedup guard skipped while a slow run was still open, is behaving as
    designed and must not be alarmed on. Zero over several periods has no benign
    reading.
    """
    now = now or datetime.now(timezone.utc)
    window = timedelta(days=window_days)
    start = now - window

    by_subject: dict[str, list[dict]] = {}
    for row in observations:
        if row.get("type") != ROW_OBSERVATION:
            continue
        sid = row.get("subject")
        if isinstance(sid, str) and sid:
            by_subject.setdefault(sid, []).append(row)

    out: list[ScheduleAudit] = []
    for s in schedules:
        if not s.enabled:
            continue
        rows = by_subject.get(s.id, [])
        stamped = [(_parse_at(r.get("at")), r) for r in rows]
        in_window = [(dt, r) for dt, r in stamped if dt is not None and start <= dt <= now]
        last = max((dt for dt, _ in stamped if dt is not None), default=None)
        expected = expected_firings(s, _exposure(s, start, now))
        observed = len(in_window)
        produced = sum(int(r.get("produced") or 0) for _, r in in_window)
        silent = observed == 0 and expected >= min_silent_periods
        reason = ""
        if silent:
            reason = (
                f"expected {expected} firing(s) in the last {window_days}d and "
                f"observed 0"
                + (f"; last firing ever: {last.isoformat()}" if last else "; never fired")
            )
        out.append(
            ScheduleAudit(
                schedule=s,
                window_start=start.isoformat(),
                window_end=now.isoformat(),
                expected=expected,
                observed=observed,
                produced=produced,
                last_observed_at=last.isoformat() if last else None,
                silent=silent,
                reason=reason,
            )
        )
    out.sort(key=lambda a: (not a.silent, a.schedule.node, a.schedule.id))
    return out


def audit_node(
    config,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_silent_periods: int = DEFAULT_MIN_SILENT_PERIODS,
    now: Optional[datetime] = None,
) -> list[ScheduleAudit]:
    """Audit one node, joining ledger observations with derived history."""
    observations = read_ledger(config.tasks_path) + derived_observations(config.tasks_path)
    return audit(
        registry(config),
        observations,
        window_days=window_days,
        min_silent_periods=min_silent_periods,
        now=now,
    )


def audit_portfolio(
    config_path: str,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_silent_periods: int = DEFAULT_MIN_SILENT_PERIODS,
    now: Optional[datetime] = None,
) -> list[ScheduleAudit]:
    """Audit this node and every registered local child."""
    schedules = portfolio_registry(config_path)
    observations: list[dict] = []
    for store in sorted({s.store for s in schedules}):
        observations.extend(read_ledger(store))
        observations.extend(derived_observations(store))
    return audit(
        schedules,
        observations,
        window_days=window_days,
        min_silent_periods=min_silent_periods,
        now=now,
    )


def exit_code(results: Iterable[ScheduleAudit]) -> int:
    """``EXIT_LIVENESS`` if any schedule is silent, else ``EXIT_OK``.

    One class, one code. This audit CANNOT return a deployment or integrity
    code, which is the structural half of the promise: a consumer that gates
    deploys on ``doctor`` can subscribe to deployment codes and adding this
    check will never change its input.
    """
    return EXIT_LIVENESS if any(r.silent for r in results) else EXIT_OK


def format_audit(results: list[ScheduleAudit]) -> str:
    """Human-readable table. Silent schedules sort first and are marked."""
    if not results:
        return "No enabled schedules found."
    header = f"{'':2} {'SCHEDULE':28} {'NODE':16} {'EVERY':6} {'EXP':>4} {'OBS':>4} {'LAST FIRED':26} FIRES"
    lines = [header, "-" * len(header)]
    for r in results:
        mark = "!!" if r.silent else "  "
        last = r.last_observed_at or "never"
        lines.append(
            f"{mark} {r.schedule.id[:28]:28} {r.schedule.node[:16]:16} "
            f"{(r.schedule.interval or '-')[:6]:6} {r.expected:>4} {r.observed:>4} "
            f"{last[:26]:26} {r.schedule.fires[:40]}"
        )
    silent = [r for r in results if r.silent]
    if silent:
        lines.append("")
        lines.append(
            f"{len(silent)} schedule(s) SILENT — consequence class "
            f"{CONSEQUENCE_CLASS!r}, exit {EXIT_LIVENESS}:"
        )
        for r in silent:
            lines.append(f"  - {r.schedule.id} ({r.schedule.node}): {r.reason}")
    return "\n".join(lines)
