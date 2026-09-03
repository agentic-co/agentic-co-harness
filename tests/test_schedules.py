"""Schedules: reservations refuse a double fire, the audit finds silent schedules.

Every time-dependent test injects a frozen clock — no real time anywhere.

The three properties under test are the three that F5 needed and v1 did not
have:

1. a period fires AT MOST ONCE, however many observers call it due;
2. a schedule that expected firings and produced none is DETECTED;
3. a DISABLED schedule is excluded — a detector that alarms on things nobody
   asked to run is a detector that stops being read.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentco_harness import schedules
from agentco_harness.beads import Beads
from agentco_harness.config import Config
from agentco_harness.recurring import Recurring, RecurringDef, reconcile

T0 = datetime(2026, 6, 1, 9, 0, 0, tzinfo=timezone.utc)


def _sched(sid="nightly-retro", interval="1d", enabled=True, node="hub", store="tasks.jsonl"):
    return schedules.Schedule(
        id=sid,
        node=node,
        interval=interval,
        cron=None,
        fires="Nightly retro",
        enabled=enabled,
        store=store,
    )


def _obs(sid, at: datetime, produced=1):
    return {
        "type": schedules.ROW_OBSERVATION,
        "subject": sid,
        "period": at.isoformat(),
        "at": at.isoformat(),
        "produced": produced,
    }


# --------------------------------------------------------------------------- #
# 1. Reservations — UNIQUE(schedule_id, period)
# --------------------------------------------------------------------------- #

def test_reservation_is_granted_once_per_period(tmp_path):
    tasks = tmp_path / "tasks.jsonl"
    first = schedules.reserve(tasks, "nightly-retro", "2026-06-01T09:00:00+00:00", now=T0)
    second = schedules.reserve(tasks, "nightly-retro", "2026-06-01T09:00:00+00:00", now=T0)
    assert first is not None
    assert second is None, "a second reservation for the same period must be refused"


def test_reservation_is_per_period_not_per_schedule(tmp_path):
    """The constraint is on the PAIR. A schedule must still fire tomorrow."""
    tasks = tmp_path / "tasks.jsonl"
    assert schedules.reserve(tasks, "nightly-retro", "2026-06-01T09:00:00+00:00", now=T0)
    assert schedules.reserve(tasks, "nightly-retro", "2026-06-02T09:00:00+00:00", now=T0)


def test_reservation_is_per_schedule_not_per_period(tmp_path):
    """Two schedules sharing a period do not collide with each other."""
    tasks = tmp_path / "tasks.jsonl"
    period = "2026-06-01T09:00:00+00:00"
    assert schedules.reserve(tasks, "nightly-retro", period, now=T0)
    assert schedules.reserve(tasks, "daily-kit", period, now=T0)


def test_reservation_survives_a_malformed_ledger_line(tmp_path):
    """A quarantined line must not silently release a held reservation.

    Rows are skipped on READ, never removed — but the skip must not lose the
    constraint, which would turn a corrupt byte into a duplicate fire.
    """
    tasks = tmp_path / "tasks.jsonl"
    period = "2026-06-01T09:00:00+00:00"
    schedules.reserve(tasks, "nightly-retro", period, now=T0)
    ledger = schedules.ledger_path(tasks)
    ledger.write_text("{not json\n" + ledger.read_text())
    assert schedules.reserve(tasks, "nightly-retro", period, now=T0) is None


def test_reconcile_refuses_to_fire_the_same_period_twice(tmp_path):
    """The regression this backport exists to prevent.

    Two observers agree a period is due. Before the reservation the second
    would fire it again; the bead-level natural key would catch the duplicate
    BEAD, but the FIRING would still have happened — which is all that matters
    for a schedule whose effect is a message or a spend.
    """
    recurring = Recurring(tmp_path / "recurring.jsonl")
    beads = Beads(tmp_path / "tasks.jsonl")
    recurring.add(
        RecurringDef(id="nightly-retro", title="Nightly retro", schedule={"every": "1d"})
    )

    spawned = reconcile(recurring, beads, now=T0)
    assert len(spawned) == 1

    # A racing observer: rewind the cursor so the SAME period looks due again.
    # This is the shape of a second cycle reading before the first one's write
    # landed, and of a `agentco cycle` run by hand alongside the heartbeat.
    recurring.update("nightly-retro", last_spawned=None)
    again = reconcile(recurring, beads, now=T0)

    assert again == [], "the period was already reserved — it must not fire twice"
    rows = schedules.read_ledger(beads.path)
    reservations = [r for r in rows if r["type"] == schedules.ROW_RESERVATION]
    assert len(reservations) == 1


def test_reconcile_records_an_observation_per_firing(tmp_path):
    recurring = Recurring(tmp_path / "recurring.jsonl")
    beads = Beads(tmp_path / "tasks.jsonl")
    recurring.add(
        RecurringDef(id="nightly-retro", title="Nightly retro", schedule={"every": "1d"})
    )
    task = reconcile(recurring, beads, now=T0)[0]

    observations = [
        r for r in schedules.read_ledger(beads.path) if r["type"] == schedules.ROW_OBSERVATION
    ]
    assert len(observations) == 1
    assert observations[0]["subject"] == "nightly-retro"
    assert observations[0]["produced"] == 1
    assert observations[0]["bead_ids"] == [task.id]


def test_observation_records_produced_zero_distinctly(tmp_path):
    """`produced: 0` is a firing. It must not read as "never fired"."""
    tasks = tmp_path / "tasks.jsonl"
    schedules.observe(tasks, "nightly-retro", "2026-06-01T09:00:00+00:00", produced=0, now=T0)
    results = schedules.audit(
        [_sched()],
        schedules.read_ledger(tasks),
        window_days=14,
        min_silent_periods=3,
        now=T0 + timedelta(days=1),
    )
    assert results[0].observed == 1
    assert results[0].produced == 0
    assert results[0].silent is False


# --------------------------------------------------------------------------- #
# 2. The audit — missed periods are detected
# --------------------------------------------------------------------------- #

def test_audit_detects_a_schedule_that_never_fired(tmp_path):
    """F5, exactly: a valid enabled daily schedule with zero runs."""
    results = schedules.audit([_sched()], [], window_days=14, min_silent_periods=3, now=T0)
    assert len(results) == 1
    assert results[0].expected == 14
    assert results[0].observed == 0
    assert results[0].silent is True
    assert results[0].last_observed_at is None
    assert "never fired" in results[0].reason
    assert schedules.exit_code(results) == schedules.EXIT_LIVENESS


def test_audit_detects_a_schedule_that_stopped_firing(tmp_path):
    """Ran happily for months, then went quiet 30 days ago."""
    long_ago = T0 - timedelta(days=30)
    results = schedules.audit(
        [_sched()], [_obs("nightly-retro", long_ago)],
        window_days=14, min_silent_periods=3, now=T0,
    )
    assert results[0].silent is True
    assert results[0].last_observed_at == long_ago.isoformat()
    assert "last firing ever" in results[0].reason


def test_audit_is_quiet_for_a_firing_schedule(tmp_path):
    observations = [_obs("nightly-retro", T0 - timedelta(days=n)) for n in range(1, 8)]
    results = schedules.audit(
        [_sched()], observations, window_days=14, min_silent_periods=3, now=T0
    )
    assert results[0].observed == 7
    assert results[0].silent is False
    assert schedules.exit_code(results) == schedules.EXIT_OK


def test_audit_does_not_alarm_on_a_single_late_period(tmp_path):
    """Alarm credibility is a resource.

    A weekly schedule inside a 14-day window expects 2 firings; that is under
    the 3-period floor, so one late run cannot raise a finding. Only ZERO
    across enough periods to have no benign reading does.
    """
    results = schedules.audit(
        [_sched(interval="7d")], [], window_days=14, min_silent_periods=3, now=T0
    )
    assert results[0].expected == 2
    assert results[0].silent is False
    assert schedules.exit_code(results) == schedules.EXIT_OK


def test_audit_never_alarms_on_partial_firing(tmp_path):
    """Fewer-than-expected is not the finding — the dedup guard legitimately
    skips periods while a slow run is still open."""
    results = schedules.audit(
        [_sched()], [_obs("nightly-retro", T0 - timedelta(days=1))],
        window_days=14, min_silent_periods=3, now=T0,
    )
    assert results[0].expected == 14
    assert results[0].observed == 1
    assert results[0].silent is False


def test_audit_ignores_observations_outside_the_window(tmp_path):
    results = schedules.audit(
        [_sched()], [_obs("nightly-retro", T0 - timedelta(days=20))],
        window_days=14, min_silent_periods=3, now=T0,
    )
    assert results[0].observed == 0
    assert results[0].silent is True


def test_audit_expects_nothing_of_an_unparseable_interval(tmp_path):
    """A quarantined definition is `recurring`'s finding, not a fake miss."""
    results = schedules.audit(
        [_sched(interval="every other tuesday")],
        [], window_days=14, min_silent_periods=3, now=T0,
    )
    assert results[0].expected == 0
    assert results[0].silent is False


def test_audit_sorts_silent_schedules_first(tmp_path):
    results = schedules.audit(
        [_sched(sid="healthy"), _sched(sid="quiet")],
        [_obs("healthy", T0 - timedelta(days=1))],
        window_days=14, min_silent_periods=3, now=T0,
    )
    assert [r.schedule.id for r in results] == ["quiet", "healthy"]


# --------------------------------------------------------------------------- #
# 3. Disabled schedules are excluded
# --------------------------------------------------------------------------- #

def test_audit_excludes_disabled_schedules(tmp_path):
    """A disabled schedule is not expected to fire, so it is not a finding.

    Excluded, not reported healthy: a row that says "0 of 0" for something
    nobody asked to run is noise, and noise is what makes a report stop
    being read.
    """
    results = schedules.audit(
        [_sched(sid="turned-off", enabled=False)],
        [], window_days=14, min_silent_periods=3, now=T0,
    )
    assert results == []
    assert schedules.exit_code(results) == schedules.EXIT_OK


def test_audit_reports_the_enabled_one_alongside_a_disabled_one(tmp_path):
    results = schedules.audit(
        [_sched(sid="turned-off", enabled=False), _sched(sid="live")],
        [], window_days=14, min_silent_periods=3, now=T0,
    )
    assert [r.schedule.id for r in results] == ["live"]
    assert results[0].silent is True


# --------------------------------------------------------------------------- #
# Registry + derived history + end-to-end through the config
# --------------------------------------------------------------------------- #

def _node(tmp_path) -> Config:
    (tmp_path / "config.yaml").write_text(f"tasks_path: {tmp_path / 'tasks.jsonl'}\n")
    return Config.load(str(tmp_path / "config.yaml"))


def test_registry_reads_the_recurring_store(tmp_path):
    config = _node(tmp_path)
    recurring = Recurring(config.recurring_path)
    recurring.add(
        RecurringDef(id="nightly-retro", title="Nightly retro", schedule={"every": "1d"},
                     agent="analyst")
    )
    recurring.add(
        RecurringDef(id="turned-off", title="Old check", schedule={"every": "1h"},
                     enabled=False)
    )
    found = {s.id: s for s in schedules.registry(config)}
    assert found["nightly-retro"].interval == "1d"
    assert found["nightly-retro"].enabled is True
    assert "analyst" in found["nightly-retro"].fires
    assert found["turned-off"].enabled is False
    assert found["nightly-retro"].node == tmp_path.name


def test_derived_observations_reconstruct_history_from_beads(tmp_path):
    """The ledger is new; the bead store is not. History must not read silent."""
    config = _node(tmp_path)
    recurring = Recurring(config.recurring_path)
    beads = Beads(config.tasks_path)
    recurring.add(
        RecurringDef(id="nightly-retro", title="Nightly retro", schedule={"every": "1d"})
    )
    for day in range(5):
        recurring.update("nightly-retro", last_spawned=None)
        task = reconcile(recurring, beads, now=T0 + timedelta(days=day))[0]
        beads.complete(task.id, result='{"status": "complete", "output": "ok"}')

    # Erase the ledger entirely: only the beads remain, as on any node whose
    # history predates this module.
    schedules.ledger_path(config.tasks_path).unlink()

    derived = schedules.derived_observations(config.tasks_path)
    assert len(derived) == 5
    assert all(r["derived"] for r in derived)

    # The reconcile clock is frozen but `created_at` is the bead store's own
    # wall clock, so the window has to be anchored to real now — which is also
    # how this reads in production.
    results = schedules.audit_node(
        config, window_days=14, now=datetime.now(timezone.utc)
    )
    assert results[0].observed == 5
    assert results[0].silent is False


def test_audit_node_finds_the_silent_schedule_end_to_end(tmp_path):
    config = _node(tmp_path)
    recurring = Recurring(config.recurring_path)
    # created_at predates the window: a schedule only "misses" firings it was
    # declared in time to make. Without this it has zero exposure at T0.
    recurring.add(
        RecurringDef(
            id="finances-intake",
            title="Monthly intake",
            schedule={"every": "1d"},
            created_at="2026-01-01T00:00:00+00:00",
        )
    )
    results = schedules.audit_node(config, window_days=14, now=T0)
    assert [r.schedule.id for r in results if r.silent] == ["finances-intake"]
    assert schedules.exit_code(results) == schedules.EXIT_LIVENESS


def test_cli_audit_exits_with_the_liveness_class(tmp_path):
    """The exit code is the interface. It must be the liveness code, never a
    generic 1 that a deployment consumer would read as fatal."""
    from click.testing import CliRunner

    from agentco_harness.cli import main

    config = _node(tmp_path)
    Recurring(config.recurring_path).add(
        RecurringDef(
            id="finances-intake",
            title="Monthly intake",
            schedule={"every": "1d"},
            created_at="2026-01-01T00:00:00+00:00",
        )
    )
    result = CliRunner().invoke(
        main, ["-c", str(tmp_path / "config.yaml"), "schedules", "audit"]
    )
    assert result.exit_code == schedules.EXIT_LIVENESS
    assert "finances-intake" in result.output
    assert "SILENT" in result.output


def test_cli_audit_exits_zero_when_everything_fires(tmp_path):
    from click.testing import CliRunner

    from agentco_harness.cli import main

    config = _node(tmp_path)
    Recurring(config.recurring_path).add(
        RecurringDef(id="quiet-weekly", title="Weekly", schedule={"every": "7d"})
    )
    result = CliRunner().invoke(
        main, ["-c", str(tmp_path / "config.yaml"), "schedules", "audit"]
    )
    assert result.exit_code == schedules.EXIT_OK


def test_reservation_key_matches_the_one_natural_key_derivation(tmp_path):
    from agentco_harness.natural_key import generated_key

    assert schedules.reservation_key("nightly-retro", "2026-06-01") == generated_key(
        "schedule", "nightly-retro", "2026-06-01"
    )
