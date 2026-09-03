"""agentco tempo — the temporal layer over the bead graph.

Beads already carries the graph (``blocked_by``), the topological frontier
(``ready()``), and the executor fork (``assigned_agent`` / ``assigned_to``).
What it has never carried is *time*: the ``Task`` model has ``created_at`` and
``updated_at`` and nothing else, so the queue can say what is ready but not
what must *start* by when, nor whether the set is achievable at all.

This module adds that, and only that. It is pure code — no LLM, no I/O, no
mutation. Every function takes a list of tasks and a clock and returns a view.

The two shapes of work
----------------------
Work is either pinned to an instant or bounded by one, and they are not the
same object:

* **PIN** (``starts_at``) — a fixed commitment. The clock is the constraint;
  the only useful behaviour is a reminder. It consumes capacity and is never
  re-ranked by urgency math.
* **DUE** (``due_at``) — a deliverable with a flexible *when*. This is what the
  scheduler ranks, via the backward pass.

Why the backward pass replaces a blocking count
-----------------------------------------------
A task that blocks other work inherits the tightest deadline of anything it
feeds::

    LF(v) = min over successors w of ( LF(w) - duration(w) )

Counting blocked tasks says *how many*; the backward pass says *how urgent*.
Blocking five far-future tasks should rank below blocking one imminent one,
and a count cannot express that. So graph leverage enters through the deadline
itself rather than as a bolt-on term.

Complexity
----------
Tempo's problem is single-resource scheduling with precedence constraints
(1|prec|Lmax) — polynomial, not the NP-hard multi-resource RCPSP. Every
function here is O(V+E). Agent-executed nodes are treated as elastic (they
scale horizontally); the human-owned subgraph is the scarce resource and the
one the ranking actually protects.

Degradation contract
--------------------
A task with no ``due_at`` and no ``estimate_hours`` scores exactly 0.0 here and
contributes nothing, so a queue with no temporal data ranks precisely as it did
before this module existed. Temporal ordering is earned by data, never imposed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .beads import Task, TaskStatus

# --- tuning -----------------------------------------------------------------
# Deadline pressure is a rational sigmoid over slack, NOT an exponential one.
# An exponential sigmoid (1/(1+e^(k(slack-s0)))) is numerically dead past ~48h:
# a task due in a week and one due in a month both score ~0 and become
# indistinguishable. The rational form below stays discriminating from minutes
# to months, which matters because most real deadlines are a week or more out.
_PRESSURE_HALFLIFE_HOURS = 8.0  # slack at which pressure = 0.5
_PRESSURE_GAMMA = 1.2  # curve steepness

# Weights. Deadline dominates but cannot drown importance.
W_DEADLINE = 0.40
W_IMPORTANCE = 0.25
W_EFFORT = 0.15
W_LEVERAGE = 0.12
W_AGE = 0.08

_AGE_SATURATION_DAYS = 30.0
_AGE_CEILING = 0.3  # capped: age nudges ties, never dominates urgency
_LEVERAGE_PER_TASK = 0.15

# An unestimated task is assumed to be this long when computing slack. Chosen
# deliberately small: over-estimating unknown work would inflate its urgency
# and let unestimated tasks crowd out measured ones.
DEFAULT_ESTIMATE_HOURS = 0.5


def _parse(stamp: str | None) -> datetime | None:
    """ISO-8601 to aware datetime. Unparseable input is None, never an error.

    Read-boundary tolerance, matching ``me._is_snoozed``: a corrupt timestamp
    must degrade one task's ranking, never poison the whole view.
    """
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp))
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def calibration_factor(tasks: list[Task]) -> float:
    """Median actual/estimate ratio over completed work. The reference class.

    Self-estimates are systematically optimistic (planning fallacy), and the
    errors are CORRELATED — someone who underestimates one task underestimates
    most of them, in the same direction. Correcting from the person's own
    history is the one mitigation with evidence behind it, and it is cheaper
    than asking anyone to estimate better.

    Median, not mean: one 10x-blown estimate must not drag every future
    schedule with it. Requires >= 3 samples before correcting at all (below
    that the "history" is noise), and clamps to [0.5, 3.0] — outside that
    range the estimates aren't miscalibrated, they're fiction, and a silent
    6x multiplier would be the model lying in the other direction.
    """
    # Pins are excluded: a synced meeting auto-completes with actual ==
    # estimate BY CONSTRUCTION (its length was a fact, not a forecast), so a
    # calendar's worth of perfect 1.0 ratios would flood the median and wash
    # out the real signal — the person's forecasting error on actual work.
    ratios = sorted(
        t.actual_hours / t.estimate_hours
        for t in tasks
        if t.status == TaskStatus.DONE
        and not is_pin(t)
        and t.actual_hours is not None
        and t.actual_hours > 0
        and t.estimate_hours is not None
        and t.estimate_hours > 0
    )
    if len(ratios) < 3:
        return 1.0
    mid = len(ratios) // 2
    median = ratios[mid] if len(ratios) % 2 else (ratios[mid - 1] + ratios[mid]) / 2.0
    return max(0.5, min(3.0, median))


def is_pin(task: Task) -> bool:
    """A fixed-time commitment: the clock is the constraint."""
    return _parse(task.starts_at) is not None


def is_due(task: Task) -> bool:
    """A deadline-bounded deliverable: the scheduler ranks this."""
    return _parse(task.due_at) is not None


def expected_hours(task: Task) -> float:
    """PERT expected duration, falling back to the point estimate.

    t_e = (o + 4m + p) / 6 when a three-point range is present. Asking a model
    for optimistic/likely/pessimistic is one prompt; asking a human for three
    numbers per task is not, so the range is optional by design.
    """
    m = task.estimate_hours
    if m is None or m <= 0:
        return DEFAULT_ESTIMATE_HOURS
    o, p = task.estimate_optimistic, task.estimate_pessimistic
    if o is not None and p is not None and o > 0 and p >= o:
        return (o + 4.0 * m + p) / 6.0
    return float(m)


def variance(task: Task) -> float:
    """PERT variance ((p-o)/6)^2. Zero when no range is given.

    Variances sum along a chain; standard deviations do not. That is what lets
    a five-task chain carry sqrt(5)x the uncertainty of a single task, and it
    is the honest answer to "estimates are unreliable" — state a probability
    instead of pretending slack is a point value.
    """
    o, p = task.estimate_optimistic, task.estimate_pessimistic
    if o is None or p is None or p < o:
        return 0.0
    return ((p - o) / 6.0) ** 2


# --- graph ------------------------------------------------------------------


def topo_order(tasks: list[Task]) -> tuple[list[str], list[str]]:
    """Kahn's algorithm. Returns (ordered ids, ids trapped in a cycle).

    A cycle is a logical contradiction — no topological order exists, so float
    is undefined on it. We never auto-break one: silently dropping an edge is
    an undisclosed data-loss decision. Cyclic ids come back so the caller can
    report them and leave them untouched.

    Edges point blocker -> blocked. Dangling ``blocked_by`` ids (a bead that
    references a task not in this set) are ignored rather than raised, matching
    the ghost-dependency tolerance in ``Beads``: a missing edge degrades one
    task's schedule, it does not hide the rest of the queue.
    """
    by_id = {t.id: t for t in tasks}
    indegree = {t.id: 0 for t in tasks}
    successors: dict[str, list[str]] = {t.id: [] for t in tasks}

    for t in tasks:
        for blocker in t.blocked_by:
            if blocker in by_id:  # dangling edges ignored, not fatal
                successors[blocker].append(t.id)
                indegree[t.id] += 1

    queue = sorted([tid for tid, d in indegree.items() if d == 0])
    order: list[str] = []
    while queue:
        tid = queue.pop(0)
        order.append(tid)
        for succ in successors[tid]:
            indegree[succ] -= 1
            if indegree[succ] == 0:
                queue.append(succ)
        queue.sort()

    cyclic = sorted(set(by_id) - set(order))
    return order, cyclic


@dataclass
class Schedule:
    """The computed temporal position of one task."""

    task_id: str
    effective_due: datetime | None  # own deadline, or the tightest inherited
    latest_start: datetime | None  # the point of no return
    slack_hours: float | None  # (effective_due - now) - work remaining
    chain_hours: float  # own effort + the longest downstream chain
    chain_sigma: float  # sqrt of summed variance along that chain
    inherited: bool = False  # deadline came from a successor, not from itself
    cyclic: bool = False

    @property
    def infeasible(self) -> bool:
        """Negative slack: the plan requires starting in the past."""
        return self.slack_hours is not None and self.slack_hours < 0

    def confidence(self) -> float | None:
        """P(this chain makes its deadline), via the normal approximation.

        Returns None when there is no deadline or no estimate range — we do not
        manufacture a probability we cannot support. With sigma = 0 this
        collapses to a hard 1.0/0.0 on the sign of slack, which is the correct
        degenerate answer for a point estimate.
        """
        if self.slack_hours is None:
            return None
        if self.chain_sigma <= 0:
            return 1.0 if self.slack_hours >= 0 else 0.0
        z = self.slack_hours / self.chain_sigma
        return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _open(tasks: list[Task]) -> list[Task]:
    return [
        t
        for t in tasks
        if t.status not in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.SKIPPED)
    ]


def schedule(
    tasks: list[Task], now: datetime | None = None, calibrate: bool = True
) -> dict[str, Schedule]:
    """Backward pass over the bead graph. O(V+E), exact, no heuristics.

    Walks reverse topological order so every task's deadline is bounded by the
    least forgiving thing it blocks. Tasks in a cycle get a Schedule with
    ``cyclic=True`` and no float — reported, never guessed at.

    ``calibrate`` applies the completed-work correction factor (median
    actual/estimate over DONE beads in this same list) to every estimate before
    slack is computed — reference-class forecasting, on by default because raw
    self-estimates are the least reliable input in the system.
    """
    now = now or datetime.now(timezone.utc)
    factor = calibration_factor(tasks) if calibrate else 1.0
    live = _open(tasks)
    by_id = {t.id: t for t in live}
    order, cyclic = topo_order(live)

    successors: dict[str, list[str]] = {t.id: [] for t in live}
    for t in live:
        for blocker in t.blocked_by:
            if blocker in by_id:
                successors[blocker].append(t.id)

    out: dict[str, Schedule] = {}

    # Reverse topological order guarantees successors are resolved first.
    for tid in reversed(order):
        task = by_id[tid]
        own_due = _parse(task.due_at)
        hours = expected_hours(task) * factor

        # LF(v) = min over successors of (LF(w) - duration(w)); own deadline
        # competes on equal terms.
        inherited_due: datetime | None = None
        chain_tail = 0.0
        tail_var = 0.0
        for succ_id in successors[tid]:
            succ = out.get(succ_id)
            if succ is None:
                continue
            if succ.latest_start is not None:
                if inherited_due is None or succ.latest_start < inherited_due:
                    inherited_due = succ.latest_start
            if succ.chain_hours > chain_tail:
                chain_tail = succ.chain_hours
                tail_var = succ.chain_sigma**2

        candidates = [d for d in (own_due, inherited_due) if d is not None]
        effective_due = min(candidates) if candidates else None
        came_from_successor = (
            effective_due is not None
            and inherited_due is not None
            and (own_due is None or inherited_due < own_due)
        )

        latest_start = (
            effective_due - timedelta(hours=hours) if effective_due else None
        )
        slack = (
            (latest_start - now).total_seconds() / 3600.0 if latest_start else None
        )

        out[tid] = Schedule(
            task_id=tid,
            effective_due=effective_due,
            latest_start=latest_start,
            slack_hours=slack,
            chain_hours=hours + chain_tail,
            chain_sigma=math.sqrt(variance(task) + tail_var),
            inherited=came_from_successor,
        )

    for tid in cyclic:
        out[tid] = Schedule(
            task_id=tid,
            effective_due=None,
            latest_start=None,
            slack_hours=None,
            chain_hours=expected_hours(by_id[tid]) * factor,
            chain_sigma=0.0,
            cyclic=True,
        )
    return out


# --- scoring ----------------------------------------------------------------


def deadline_pressure(slack_hours: float | None) -> float:
    """Rational sigmoid over slack, in [0, 1].

    Saturates at 1.0 once slack is negative (already impossible) and decays
    smoothly but never to zero, so a task due next month still outranks one
    with no deadline at all.
    """
    if slack_hours is None:
        return 0.0
    if slack_hours <= 0:
        return 1.0
    ratio = slack_hours / _PRESSURE_HALFLIFE_HOURS
    return 1.0 / (1.0 + ratio**_PRESSURE_GAMMA)


def effort_term(hours: float | None) -> float:
    """Quick-win bonus, normalised to [0, 1].

    Every term feeding the composite MUST be bounded, or the weights are
    meaningless. An unbounded 1/ln(1+h) reaches 12.5 for a five-minute task and
    lets trivia outrank a critical overdue item by 4.7x — normalisation is the
    whole point of this function.
    """
    if hours is None or hours <= 0:
        return 0.5  # unknown effort is neutral, not maximally attractive
    return 1.0 / (1.0 + math.log1p(hours))


def age_term(age_seconds: float) -> float:
    """Capped anti-starvation bump, in [0, _AGE_CEILING].

    Mirrors the rationale already in ``me._age_factor``: old items rise but
    never drown a fresh critical failure.
    """
    days = max(age_seconds, 0.0) / 86400.0
    return min(1.0, days / _AGE_SATURATION_DAYS) * _AGE_CEILING


def temporal_score(
    task: Task,
    sched: Schedule | None,
    importance: float,
    leverage: int,
    age_seconds: float,
) -> float:
    """Composite urgency in roughly [0, 1]. Zero when there is no time data.

    ``importance`` is normalised 0..1 by the caller from whatever priority
    signal it owns — Tempo deliberately does NOT introduce a second priority
    field, because a queue with two of them inflates both.
    """
    if sched is None or sched.slack_hours is None:
        # No deadline anywhere in this task's future: contribute nothing and
        # let the caller's existing ranking stand untouched.
        return 0.0
    return (
        W_DEADLINE * deadline_pressure(sched.slack_hours)
        + W_IMPORTANCE * max(0.0, min(1.0, importance))
        + W_EFFORT * effort_term(expected_hours(task))
        + W_LEVERAGE * min(1.0, _LEVERAGE_PER_TASK * max(0, leverage))
        + W_AGE * age_term(age_seconds)
    )


def explain(task: Task, sched: Schedule | None) -> str:
    """One sentence naming the one or two dominant drivers.

    Deliberately not a table of five weighted terms: a full breakdown is not a
    sentence and defeats the purpose. Taskwarrior proved that exposing a raw
    float is necessary but not sufficient — users could not reason about the
    units even reading the source — so this speaks in hours.
    """
    if sched is None or sched.slack_hours is None:
        return "no deadline — ranked on standing priority"

    hours = expected_hours(task)
    parts: list[str] = []
    if sched.slack_hours < 0:
        parts.append(f"{abs(sched.slack_hours):.1f}h past the point of no return")
    else:
        parts.append(f"{hours:.1f}h of work, {sched.slack_hours:.1f}h of slack")
    if sched.inherited:
        parts.append("deadline inherited from work it blocks")
    conf = sched.confidence()
    if conf is not None and sched.chain_sigma > 0:
        parts.append(f"{conf * 100:.0f}% likely to make it")
    return "; ".join(parts[:2])


# --- feasibility ------------------------------------------------------------


@dataclass
class Feasibility:
    """Whether the deadline-bound set is achievable, and what slips if not."""

    committed_hours: float
    available_hours: float
    slip: list[str] = field(default_factory=list)
    cyclic: list[str] = field(default_factory=list)

    @property
    def feasible(self) -> bool:
        return not self.slip

    @property
    def overload_ratio(self) -> float:
        if self.available_hours <= 0:
            return float("inf") if self.committed_hours > 0 else 0.0
        return self.committed_hours / self.available_hours


def productive_hours_between(
    now: datetime, until: datetime, hours_per_day: float
) -> float:
    """Working hours available in a window, without a calendar.

    Whole days contribute ``hours_per_day``; the partial remainder contributes
    its wall-clock hours capped at ``hours_per_day``. Deliberately optimistic
    for short windows — spreading a 6h day uniformly across 24h would claim you
    only have 1 productive hour before a deadline 4h away, which is nonsense
    and would fire false infeasibility constantly.

    Biasing optimistic is the right error direction for a feasibility floor:
    we only want to say "impossible" when it genuinely is. This is the crudest
    honest model available until Tempo owns the calendar and can subtract real
    committed time.
    """
    total = (until - now).total_seconds() / 3600.0
    if total <= 0:
        return 0.0
    full_days = int(total // 24)
    remainder = total - full_days * 24
    return full_days * hours_per_day + min(remainder, hours_per_day)


def feasibility(
    tasks: list[Task],
    now: datetime | None = None,
    hours_per_day: float = 6.0,
    horizon_days: float = 14.0,
) -> Feasibility:
    """Greedy earliest-deadline-first fit. Whatever does not fit is the slip list.

    EDF is provably optimal for single-resource feasibility, so this is not a
    heuristic guess: **if this ordering cannot fit the work, no ordering can.**
    That is what licenses stating the result plainly rather than hedging.

    PINs consume capacity before anything flexible is placed — a meeting does
    not merely occupy an hour, it steals an hour from every deadline downstream
    of it. Agent-owned tasks are excluded: they scale horizontally and are a
    queueing problem, not a claim on the human's day.
    """
    now = now or datetime.now(timezone.utc)
    live = _open(tasks)
    # Pass the FULL list, not `live`: schedule() filters open tasks itself, and
    # the calibration factor needs the DONE beads — filtering first would starve
    # the calibrator of its entire reference class and silently pin it at 1.0.
    scheds = schedule(tasks, now=now)
    factor = calibration_factor(tasks)
    horizon_end = now + timedelta(days=horizon_days)

    human = [t for t in live if not _is_agent_owned(t)]
    # Pins are NOT calibrated: a meeting's duration is its scheduled length,
    # a fact — not a self-estimate subject to the planning fallacy.
    pinned = sum(
        expected_hours(t)
        for t in human
        if is_pin(t) and (_parse(t.starts_at) or horizon_end) <= horizon_end
    )
    available = max(
        0.0, productive_hours_between(now, horizon_end, hours_per_day) - pinned
    )

    deadline_bound = [
        t
        for t in human
        if not is_pin(t)
        and (s := scheds.get(t.id)) is not None
        and s.effective_due is not None
        and s.effective_due <= horizon_end
    ]
    deadline_bound.sort(key=lambda t: scheds[t.id].effective_due)  # type: ignore[arg-type,index]

    # Greedy earliest-deadline-first. EDF is provably optimal for
    # single-resource feasibility, so anything that does not fit here does not
    # fit under ANY ordering — that is what licenses reporting it as fact.
    #
    # Pins are subtracted proportionally: they are already-spent hours, so a
    # task cannot claim capacity a meeting has taken. Approximated by scaling
    # the window's raw capacity by the fraction of the horizon left unpinned.
    horizon_capacity = productive_hours_between(now, horizon_end, hours_per_day)
    unpinned_fraction = (
        (available / horizon_capacity) if horizon_capacity > 0 else 0.0
    )

    consumed = 0.0
    slip: list[str] = []
    for t in deadline_bound:
        sched = scheds[t.id]
        hours = expected_hours(t) * factor
        capacity_by_due = (
            productive_hours_between(now, sched.effective_due, hours_per_day)  # type: ignore[arg-type]
            * unpinned_fraction
        )
        if consumed + hours > capacity_by_due:
            slip.append(t.id)
        else:
            consumed += hours

    return Feasibility(
        committed_hours=round(sum(expected_hours(t) * factor for t in deadline_bound), 3),
        available_hours=round(available, 3),
        slip=slip,
        cyclic=topo_order(live)[1],
    )


def _is_agent_owned(task: Task) -> bool:
    """Agent capacity is elastic; only human-owned work claims the scarce day."""
    return task.assigned_to is None and task.assigned_agent is not None
