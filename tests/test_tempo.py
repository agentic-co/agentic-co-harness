"""Tests for agentco_harness.tempo — the temporal layer over the bead graph.

The scoring math is the whole product here: every surface downstream is a
renderer for these functions, so a wrong ordering is not a cosmetic bug. These
tests pin the properties that must hold, not just the happy path.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentco_harness.beads import Beads, TaskPriority, TaskStatus
from agentco_harness.me import ranked
from agentco_harness.tempo import (
    DEFAULT_ESTIMATE_HOURS,
    deadline_pressure,
    effort_term,
    expected_hours,
    feasibility,
    schedule,
    temporal_score,
    topo_order,
    variance,
)

NOW = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)


def _beads(tmp_path: Path) -> Beads:
    return Beads(tmp_path / "tasks.jsonl")


def _iso(**delta) -> str:
    return (NOW + timedelta(**delta)).isoformat()


def _forge_ghost_blocker(beads: Beads, task_id: str, ghost: str) -> None:
    """Point ``task_id`` at a nonexistent blocker by rewriting the store.

    The write boundary refuses ghost blockers now (TaskReferenceError), so a
    dangling edge can only arrive by hand edit, bad merge, or legacy data.
    Tempo must still tolerate those — that is what these tests pin — so the
    fixture writes the line rather than calling update().
    """
    lines = []
    for line in beads.path.read_text().splitlines():
        record = json.loads(line)
        if record["id"] == task_id:
            record["blocked_by"] = [ghost]
        lines.append(json.dumps(record))
    beads.path.write_text("\n".join(lines) + "\n")


# --- term properties --------------------------------------------------------


def test_effort_term_is_bounded():
    """Every term feeding the composite must be in [0,1] or weights are noise.

    The regression this guards: an unbounded 1/ln(1+h) reaches 12.5 for a
    five-minute task, which is 4.7x the entire maximum deadline contribution —
    trivia outranks a critical overdue item and the model inverts.
    """
    for hours in [0.001, 0.083, 0.25, 1, 2, 8, 40, 500]:
        assert 0.0 <= effort_term(hours) <= 1.0
    # Shorter work scores higher, monotonically.
    assert effort_term(0.25) > effort_term(1) > effort_term(8) > effort_term(40)


def test_effort_term_handles_zero_and_none():
    """effort=0 must not divide by zero — it is the majority case for unestimated beads."""
    assert 0.0 <= effort_term(0) <= 1.0
    assert 0.0 <= effort_term(None) <= 1.0
    assert 0.0 <= effort_term(-5) <= 1.0


def test_deadline_pressure_bounded_and_monotonic():
    assert deadline_pressure(None) == 0.0
    assert deadline_pressure(-100) == 1.0  # already impossible saturates
    assert deadline_pressure(0) == 1.0
    prev = 1.1
    for slack in [0.5, 2, 8, 24, 72, 168, 720]:
        p = deadline_pressure(slack)
        assert 0.0 < p < 1.0
        assert p < prev, f"pressure must decrease as slack grows (at {slack}h)"
        prev = p


def test_deadline_pressure_still_discriminates_past_two_days():
    """The exponential-sigmoid regression: everything past ~48h scored ~0.

    A task due next week and one due next month were numerically identical,
    which made the 0.40 deadline weight inert for anyone whose deadlines are
    a week out — i.e. most people.
    """
    week, month = deadline_pressure(168), deadline_pressure(720)
    assert week > month > 0.0
    assert week / month > 2.0, "a week vs a month must be clearly distinguishable"


def test_age_term_is_capped():
    from agentco_harness.tempo import _AGE_CEILING, age_term

    assert age_term(0) == 0.0
    assert age_term(86400 * 10_000) <= _AGE_CEILING


# --- the inversion regression ----------------------------------------------


def test_trivial_task_does_not_outrank_critical_overdue(tmp_path):
    """The headline bug: a 5-minute no-deadline task scored 1.93 and a
    24h-overdue critical task blocking two others scored 0.78. It inverted."""
    b = _beads(tmp_path)
    trivial = b.create(
        "trivial", "", priority=TaskPriority.LOW, estimate_hours=0.083
    )
    critical = b.create(
        "critical",
        "",
        priority=TaskPriority.CRITICAL,
        estimate_hours=4,
        due_at=_iso(hours=-24),
    )
    scheds = schedule(b.list(), now=NOW)

    trivial_score = temporal_score(
        trivial, scheds.get(trivial.id), importance=0.15, leverage=0, age_seconds=86400
    )
    critical_score = temporal_score(
        critical, scheds.get(critical.id), importance=1.0, leverage=2, age_seconds=86400
    )
    assert critical_score > trivial_score
    # And the no-deadline task contributes nothing at all.
    assert trivial_score == 0.0


# --- graph ------------------------------------------------------------------


def test_topo_order_and_cycle_detection(tmp_path):
    b = _beads(tmp_path)
    a = b.create("a", "")
    c = b.create("c", "", blocked_by=[a.id])
    order, cyclic = topo_order(b.list())
    assert order.index(a.id) < order.index(c.id)
    assert cyclic == []


def test_closing_a_cycle_is_refused_at_write_time(tmp_path):
    """Prevention beats detection: the offending edge is still in hand and can
    be named. An undetected cycle is a SILENT deadlock — every member waits on
    another forever, nothing is stale, nothing errors, the work just never
    happens."""
    import pytest

    from agentco_harness.beads import DependencyCycleError

    b = _beads(tmp_path)
    a = b.create("a", "")
    c = b.create("c", "", blocked_by=[a.id])
    with pytest.raises(DependencyCycleError) as exc:
        b.update(a.id, blocked_by=[c.id])
    # The error must name the actual chain — "there is a cycle" is not actionable.
    assert a.id in str(exc.value) and c.id in str(exc.value)
    # And the queue is untouched: we never auto-break an edge.
    assert b.get(a.id).blocked_by == []


def test_self_block_is_refused(tmp_path):
    import pytest

    from agentco_harness.beads import DependencyCycleError

    b = _beads(tmp_path)
    a = b.create("a", "")
    with pytest.raises(DependencyCycleError):
        b.update(a.id, blocked_by=[a.id])


def test_ghost_blocker_is_not_mistaken_for_a_cycle(tmp_path):
    b = _beads(tmp_path)
    a = b.create("a", "")
    _forge_ghost_blocker(b, a.id, "ac-deadbeef")
    # The cycle walk must ignore the dangling id, not mistake it for a loop.
    assert b.get(a.id).blocked_by == ["ac-deadbeef"]
    order, cyclic = topo_order(b.list())
    assert cyclic == []


def test_legacy_cycle_on_disk_is_reported_never_broken(tmp_path):
    """The guard stops NEW cycles; data written before it existed may still
    contain one. Tempo must report those, not crash and not silently repair."""
    path = tmp_path / "tasks.jsonl"
    path.write_text(
        '{"id":"a","title":"a","description":"","status":"pending","priority":2,'
        '"blocked_by":["c"],"metadata":{}}\n'
        '{"id":"c","title":"c","description":"","status":"pending","priority":2,'
        '"blocked_by":["a"],"metadata":{}}\n'
    )
    tasks = Beads(path).list()
    order, cyclic = topo_order(tasks)
    assert set(cyclic) == {"a", "c"}
    assert order == []
    scheds = schedule(tasks, now=NOW)
    assert scheds["a"].cyclic is True
    assert scheds["a"].slack_hours is None


def test_dangling_blocked_by_is_tolerated(tmp_path):
    """Ghost dependencies must degrade one bead, never hide the queue."""
    b = _beads(tmp_path)
    t = b.create("orphan", "")
    _forge_ghost_blocker(b, t.id, "ac-deadbeef")
    order, cyclic = topo_order(b.list())
    assert order == [t.id]
    assert cyclic == []


def test_deadline_is_inherited_from_blocked_work(tmp_path):
    """The backward pass, and the reason it replaces a blocking count.

    A blocker with no deadline of its own inherits the latest start of the
    thing it blocks. Counting says 'blocks 1'; this says *how urgent*.
    """
    b = _beads(tmp_path)
    blocker = b.create("blocker", "", estimate_hours=2)
    blocked = b.create(
        "blocked", "", estimate_hours=3, due_at=_iso(hours=10), blocked_by=[blocker.id]
    )
    scheds = schedule(b.list(), now=NOW)

    # blocked: due in 10h, needs 3h → latest start 7h from now
    assert abs(scheds[blocked.id].slack_hours - 7.0) < 0.01
    # blocker: must finish by blocked's latest start (7h), needs 2h → 5h slack
    assert scheds[blocker.id].inherited is True
    assert abs(scheds[blocker.id].slack_hours - 5.0) < 0.01
    # The blocker is now MORE urgent than the thing it blocks.
    assert scheds[blocker.id].slack_hours < scheds[blocked.id].slack_hours


def test_tightest_downstream_deadline_wins(tmp_path):
    """Blocking five far-future tasks must rank below blocking one imminent one."""
    b = _beads(tmp_path)
    blocker = b.create("blocker", "", estimate_hours=1)
    b.create("far", "", estimate_hours=1, due_at=_iso(days=30), blocked_by=[blocker.id])
    b.create("near", "", estimate_hours=1, due_at=_iso(hours=6), blocked_by=[blocker.id])
    scheds = schedule(b.list(), now=NOW)
    # Inherits from 'near' (latest start 5h), minus its own 1h → 4h slack.
    assert abs(scheds[blocker.id].slack_hours - 4.0) < 0.01


def test_chain_length_accumulates_along_longest_path(tmp_path):
    b = _beads(tmp_path)
    a = b.create("a", "", estimate_hours=1)
    mid = b.create("mid", "", estimate_hours=2, blocked_by=[a.id])
    b.create("end", "", estimate_hours=4, blocked_by=[mid.id])
    scheds = schedule(b.list(), now=NOW)
    assert scheds[a.id].chain_hours == 7.0


def test_done_tasks_leave_the_graph(tmp_path):
    b = _beads(tmp_path)
    a = b.create("a", "", estimate_hours=1)
    c = b.create("c", "", estimate_hours=1, due_at=_iso(hours=5), blocked_by=[a.id])
    b.update(a.id, status=TaskStatus.DONE)
    scheds = schedule(b.list(), now=NOW)
    assert a.id not in scheds
    assert abs(scheds[c.id].slack_hours - 4.0) < 0.01


# --- PERT -------------------------------------------------------------------


def test_pert_expected_and_variance(tmp_path):
    b = _beads(tmp_path)
    t = b.create(
        "t", "", estimate_hours=4, estimate_optimistic=2, estimate_pessimistic=12
    )
    assert abs(expected_hours(t) - (2 + 16 + 12) / 6) < 1e-9
    assert abs(variance(t) - ((12 - 2) / 6) ** 2) < 1e-9


def test_point_estimate_has_no_variance(tmp_path):
    b = _beads(tmp_path)
    t = b.create("t", "", estimate_hours=4)
    assert expected_hours(t) == 4.0
    assert variance(t) == 0.0


def test_unestimated_task_uses_default(tmp_path):
    b = _beads(tmp_path)
    t = b.create("t", "")
    assert expected_hours(t) == DEFAULT_ESTIMATE_HOURS


def test_confidence_reflects_chain_uncertainty(tmp_path):
    """Variances sum along a chain, so a longer chain is less certain."""
    b = _beads(tmp_path)
    a = b.create("a", "", estimate_hours=2, estimate_optimistic=1, estimate_pessimistic=6)
    b.create(
        "z",
        "",
        estimate_hours=2,
        estimate_optimistic=1,
        estimate_pessimistic=6,
        due_at=_iso(hours=8),
        blocked_by=[a.id],
    )
    scheds = schedule(b.list(), now=NOW)
    conf = scheds[a.id].confidence()
    assert conf is not None and 0.0 < conf < 1.0
    assert scheds[a.id].chain_sigma > 0


# --- feasibility ------------------------------------------------------------


def test_productive_hours_does_not_spread_a_workday_across_the_night():
    """Regression: capacity was modelled as hours_per_day spread uniformly over
    all 24h, so a deadline 4h away granted only 1 productive hour and a 3h task
    was declared impossible. That fires false infeasibility constantly, which is
    the one thing a trust-dependent ranking cannot afford."""
    from agentco_harness.tempo import productive_hours_between

    assert productive_hours_between(NOW, NOW + timedelta(hours=4), 6.0) == 4.0
    assert productive_hours_between(NOW, NOW + timedelta(hours=8), 6.0) == 6.0
    assert productive_hours_between(NOW, NOW + timedelta(days=2), 6.0) == 12.0
    assert productive_hours_between(NOW, NOW - timedelta(hours=1), 6.0) == 0.0


def test_three_hours_of_work_due_in_four_hours_is_feasible(tmp_path):
    b = _beads(tmp_path)
    b.create("tax return", "", estimate_hours=3, due_at=_iso(hours=4))
    result = feasibility(b.list(), now=NOW, hours_per_day=6, horizon_days=14)
    assert result.feasible, "3h of work with a 4h window must not be called impossible"


def test_feasible_queue_has_no_slip(tmp_path):
    b = _beads(tmp_path)
    b.create("small", "", estimate_hours=1, due_at=_iso(days=5))
    result = feasibility(b.list(), now=NOW, hours_per_day=6, horizon_days=14)
    assert result.feasible
    assert result.slip == []


def test_impossible_queue_names_what_slips(tmp_path):
    """EDF is provably optimal for single-resource feasibility, so if this
    ordering cannot fit the work, no ordering can. That is what licenses
    stating the result plainly instead of hedging."""
    b = _beads(tmp_path)
    for i in range(6):
        b.create(f"big-{i}", "", estimate_hours=20, due_at=_iso(days=1))
    result = feasibility(b.list(), now=NOW, hours_per_day=6, horizon_days=14)
    assert not result.feasible
    assert len(result.slip) > 0
    assert result.overload_ratio > 1.0


def test_agent_owned_work_does_not_consume_human_capacity(tmp_path):
    """Agent capacity is elastic — it scales horizontally. Only human-owned
    work claims the one scarce resource."""
    b = _beads(tmp_path)
    for i in range(10):
        b.create(
            f"agent-{i}",
            "",
            assigned_agent="worker",
            estimate_hours=20,
            due_at=_iso(days=1),
        )
    result = feasibility(b.list(), now=NOW, hours_per_day=6, horizon_days=14)
    assert result.feasible, "agent work must not blow the human's capacity check"


def test_pins_consume_capacity(tmp_path):
    """A meeting doesn't merely occupy an hour — it steals an hour from every
    deadline downstream of it."""
    b = _beads(tmp_path)
    b.create("standup", "", starts_at=_iso(hours=2), estimate_hours=1)
    free = feasibility(b.list(), now=NOW, hours_per_day=6, horizon_days=2).available_hours
    b.create("workshop", "", starts_at=_iso(hours=5), estimate_hours=3)
    less = feasibility(b.list(), now=NOW, hours_per_day=6, horizon_days=2).available_hours
    assert less == free - 3.0, "a pinned meeting must remove its hours from capacity"
    assert less < free


# --- degradation contract ---------------------------------------------------


def test_queue_with_no_temporal_data_ranks_exactly_as_before(tmp_path):
    """The safety floor: adopting tempo must not silently re-order existing
    queues. No due dates anywhere → multiplier is exactly 1.0."""
    root = tmp_path / "co"
    root.mkdir()
    (root / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    b = Beads(root / "tasks.jsonl")
    b.create("failed one", "", status=TaskStatus.FAILED)
    b.create("approval one", "", status=TaskStatus.PENDING_APPROVAL)

    items = ranked(str(root / "config.yaml"), now=NOW)
    assert items, "expected human-gated items"
    for item in items:
        assert item.temporal == 0.0
        assert item.slack_hours is None
        assert item.why == "no deadline — ranked on standing priority"


def test_deadline_lifts_an_item_within_its_severity(tmp_path):
    root = tmp_path / "co"
    root.mkdir()
    (root / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    b = Beads(root / "tasks.jsonl")
    b.create("no deadline", "", assigned_to="human:m", estimate_hours=1)
    urgent = b.create(
        "urgent",
        "",
        assigned_to="human:m",
        estimate_hours=1,
        due_at=_iso(hours=1),
    )
    items = ranked(str(root / "config.yaml"), now=NOW)
    assert items[0].task_id == urgent.id
    assert items[0].temporal > 0.0
    assert "slack" in items[0].why or "point of no return" in items[0].why


# --- CLI reachability (the fields must be settable without writing Python) ---


def test_cli_create_with_due_and_estimate(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from agentco_harness.cli import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    r = CliRunner().invoke(
        main,
        ["--config", "config.yaml", "tasks", "create", "File taxes",
         "--due", "2026-08-08T17:00", "--estimate", "3",
         "--estimate-range", "2", "6"],
    )
    assert r.exit_code == 0, r.output
    t = Beads(tmp_path / "tasks.jsonl").list()[0]
    assert t.due_at == "2026-08-08T17:00"
    assert t.estimate_hours == 3.0
    assert t.estimate_optimistic == 2.0 and t.estimate_pessimistic == 6.0


def test_cli_create_refuses_pin_plus_due(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from agentco_harness.cli import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    r = CliRunner().invoke(
        main,
        ["--config", "config.yaml", "tasks", "create", "x",
         "--due", "2026-08-08T17:00", "--starts-at", "2026-08-08T09:00"],
    )
    assert r.exit_code != 0
    assert "mutually exclusive" in r.output


def test_cli_create_refuses_unparseable_due(tmp_path, monkeypatch):
    """A silently unparseable deadline would rank as 'no deadline' — the exact
    opposite of what the user asked for. Refuse loudly at the boundary."""
    from click.testing import CliRunner

    from agentco_harness.cli import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    r = CliRunner().invoke(
        main,
        ["--config", "config.yaml", "tasks", "create", "x", "--due", "friday"],
    )
    assert r.exit_code != 0
    assert "ISO-8601" in r.output


def test_cli_update_clears_blockers_as_doctor_suggests(tmp_path, monkeypatch):
    """The doctor cycle message names `tasks update <id> --clear-blocked-by` as
    the fix. A resolve hint pointing at a nonexistent command is a manufactured
    false diagnosis — this pins that the command exists and does what it says."""
    from click.testing import CliRunner

    from agentco_harness.cli import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    b = Beads(tmp_path / "tasks.jsonl")
    a = b.create("a", "")
    c = b.create("c", "", blocked_by=[a.id])
    r = CliRunner().invoke(
        main,
        ["--config", "config.yaml", "tasks", "update", c.id, "--clear-blocked-by"],
    )
    assert r.exit_code == 0, r.output
    assert Beads(tmp_path / "tasks.jsonl").get(c.id).blocked_by == []


def test_cli_update_refuses_cycle(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from agentco_harness.cli import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    b = Beads(tmp_path / "tasks.jsonl")
    a = b.create("a", "")
    c = b.create("c", "", blocked_by=[a.id])
    r = CliRunner().invoke(
        main,
        ["--config", "config.yaml", "tasks", "update", a.id, "--blocked-by", c.id],
    )
    assert r.exit_code != 0
    assert "cycle" in r.output.lower()
    assert Beads(tmp_path / "tasks.jsonl").get(a.id).blocked_by == []


# --- calibration ------------------------------------------------------------


def test_calibration_needs_three_samples(tmp_path):
    """Below 3 completions the 'history' is noise — no correction."""
    from agentco_harness.tempo import calibration_factor

    b = _beads(tmp_path)
    for _ in range(2):
        t = b.create("t", "", estimate_hours=1)
        b.update(t.id, status=TaskStatus.DONE, actual_hours=3.0)
    assert calibration_factor(b.list()) == 1.0


def test_calibration_uses_median_not_mean(tmp_path):
    """One 10x-blown estimate must not drag every future schedule with it."""
    from agentco_harness.tempo import calibration_factor

    b = _beads(tmp_path)
    for actual in (1.5, 1.5, 1.5, 15.0):  # three honest 1.5x, one disaster
        t = b.create("t", "", estimate_hours=1)
        b.update(t.id, status=TaskStatus.DONE, actual_hours=actual)
    assert calibration_factor(b.list()) == 1.5


def test_calibration_is_clamped(tmp_path):
    """Outside [0.5, 3.0] the estimates aren't miscalibrated, they're fiction —
    a silent 6x multiplier would be the model lying in the other direction."""
    from agentco_harness.tempo import calibration_factor

    b = _beads(tmp_path)
    for _ in range(3):
        t = b.create("t", "", estimate_hours=1)
        b.update(t.id, status=TaskStatus.DONE, actual_hours=10.0)
    assert calibration_factor(b.list()) == 3.0


def test_schedule_applies_calibration_from_history(tmp_path):
    """The point of the whole loop: slack shrinks when your history says your
    estimates run hot. 1h estimated at a 2x ratio = 2h expected."""
    b = _beads(tmp_path)
    for _ in range(3):
        t = b.create("done", "", estimate_hours=1)
        b.update(t.id, status=TaskStatus.DONE, actual_hours=2.0)
    open_task = b.create("open", "", estimate_hours=1, due_at=_iso(hours=10))

    calibrated = schedule(b.list(), now=NOW)[open_task.id]
    raw = schedule(b.list(), now=NOW, calibrate=False)[open_task.id]
    assert abs(raw.slack_hours - 9.0) < 0.01
    assert abs(calibrated.slack_hours - 8.0) < 0.01  # 10h - 1h*2.0


def test_feasibility_calibration_sees_done_history(tmp_path):
    """Regression guard: feasibility must hand schedule() the FULL list — the
    DONE beads ARE the reference class, and pre-filtering them silently pins
    the correction at 1.0 forever."""
    b = _beads(tmp_path)
    for _ in range(3):
        t = b.create("done", "", estimate_hours=1)
        b.update(t.id, status=TaskStatus.DONE, actual_hours=3.0)
    # 4h of estimated work due in 8h: fine raw, infeasible at the honest 3x.
    b.create("open", "", estimate_hours=4, due_at=_iso(hours=8))
    result = feasibility(b.list(), now=NOW, hours_per_day=6, horizon_days=14)
    assert not result.feasible


def test_cli_complete_records_actual(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from agentco_harness.cli import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    b = Beads(tmp_path / "tasks.jsonl")
    t = b.create("t", "", estimate_hours=2)
    r = CliRunner().invoke(
        main,
        ["--config", "config.yaml", "tasks", "complete", t.id, "--actual", "3.5"],
    )
    assert r.exit_code == 0, r.output
    done = Beads(tmp_path / "tasks.jsonl").get(t.id)
    assert done.status == TaskStatus.DONE
    assert done.actual_hours == 3.5


# --- portfolio feasibility --------------------------------------------------


def test_portfolio_tasks_walks_children(tmp_path):
    """The graph is per-company; the hours are one person's. Feasibility is
    only true at the portfolio level."""
    import json as _json

    from agentco_harness.me import portfolio_tasks

    parent = tmp_path / "parent"
    child = tmp_path / "child"
    for d in (parent, child):
        d.mkdir()
        (d / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    Beads(parent / "tasks.jsonl").create("parent work", "", estimate_hours=1)
    Beads(child / "tasks.jsonl").create("child work", "", estimate_hours=1)
    reg = parent / "children" / "registry.jsonl"
    reg.parent.mkdir(parents=True)
    reg.write_text(
        _json.dumps(
            {"name": "kid", "path": str(child), "expected_interval": "1h", "notify": False}
        )
        + "\n"
    )

    titles = {t.title for t in portfolio_tasks(str(parent / "config.yaml"))}
    assert titles == {"parent work", "child work"}


def test_portfolio_tasks_survives_a_broken_child(tmp_path, capsys):
    """One broken node must not hide the rest — and the warning must say the
    check is now optimistic, because missing load is the direction it errs."""
    import json as _json

    from agentco_harness.me import portfolio_tasks

    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    Beads(parent / "tasks.jsonl").create("parent work", "")
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "config.yaml").write_text(":: not yaml ::")
    reg = parent / "children" / "registry.jsonl"
    reg.parent.mkdir(parents=True)
    reg.write_text(
        _json.dumps(
            {"name": "bad", "path": str(broken), "expected_interval": "1h", "notify": False}
        )
        + "\n"
    )

    tasks = portfolio_tasks(str(parent / "config.yaml"))
    assert {t.title for t in tasks} == {"parent work"}
    assert "optimistic" in capsys.readouterr().err


# --- planner temporal proposals ---------------------------------------------


def test_planner_decompose_chains_and_estimates_and_gates_parent(tmp_path):
    """The full wiring: sequential chains siblings, estimates land on the
    beads, and the parent becomes blocked_by its parts — which is what makes
    the parent's due_at propagate through the chain via the backward pass."""
    from agentco_harness.config import Config as _Config
    from agentco_harness.orchestrator import Orchestrator

    (tmp_path / "config.yaml").write_text(f"tasks_path: {tmp_path}/tasks.jsonl\n")
    config = _Config.load(str(tmp_path / "config.yaml"))
    orch = Orchestrator.__new__(Orchestrator)  # skip LM setup
    orch.config = config
    orch.beads = Beads(config.tasks_path)
    orch._notify_planner_decision = lambda *a, **k: None

    parent = orch.beads.create(
        "File Q3 taxes", "", due_at=_iso(hours=20), estimate_hours=0.1
    )
    ok = orch._planner_decompose(
        parent,
        {
            "decision": "decompose",
            "sequential": True,
            "subtasks": [
                {"title": "Gather receipts", "estimate_hours": 2,
                 "estimate_optimistic": 1, "estimate_pessimistic": 4},
                {"title": "Reconcile", "estimate_hours": "3"},
                {"title": "Submit", "estimate_hours": "garbage"},
            ],
        },
    )
    assert ok
    tasks = {t.title: t for t in orch.beads.list()}
    gather, reconcile, submit = (
        tasks["Gather receipts"], tasks["Reconcile"], tasks["Submit"]
    )
    # Estimates: numeric parsed (even from a string), garbage degrades to None.
    assert gather.estimate_hours == 2.0 and gather.estimate_pessimistic == 4.0
    assert reconcile.estimate_hours == 3.0
    assert submit.estimate_hours is None
    # Sequential chain.
    assert reconcile.blocked_by == [gather.id]
    assert submit.blocked_by == [reconcile.id]
    # Parent gated on all three.
    parent_now = orch.beads.get(parent.id)
    assert set(parent_now.blocked_by) == {gather.id, reconcile.id, submit.id}
    # And the payoff: the first link inherits the parent's deadline backward
    # through the whole chain. 20h due - 0.1h parent - 0.5h submit(default)
    # - 3h reconcile - 2h gather => positive but tight slack for gather.
    scheds = schedule(orch.beads.list(), now=NOW)
    assert scheds[gather.id].inherited
    assert scheds[gather.id].slack_hours is not None
    assert scheds[gather.id].slack_hours < scheds[submit.id].slack_hours
