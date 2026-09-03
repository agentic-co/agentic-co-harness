"""Generator unit tests: overdue math, catch_up, dedup, crash window, quarantine.

All time-dependent tests inject a frozen clock — no real time anywhere.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest

from agentco_harness.beads import Beads, TaskStatus
from agentco_harness.recurring import (
    Recurring,
    RecurringDef,
    parse_duration,
    reconcile,
    supersede_resolved_rcas,
    supersede_stale_failures,
)

T0 = datetime(2026, 6, 1, 9, 0, 0, tzinfo=timezone.utc)


def _store(tmp_path, **kwargs) -> tuple[Recurring, Beads]:
    recurring = Recurring(tmp_path / "recurring.jsonl")
    beads = Beads(tmp_path / "tasks.jsonl")
    if kwargs:
        recurring.add(RecurringDef(**kwargs))
    return recurring, beads


def _weekly(last_spawned: str | None = None, **overrides) -> dict:
    d = dict(
        id="rec-001",
        title="Weekly KPI report",
        schedule={"every": "7d"},
        agent="analyst",
        last_spawned=last_spawned,
    )
    d.update(overrides)
    return d


# ---------------------------------------------------------------- durations


def test_parse_duration_units():
    assert parse_duration("15m") == timedelta(minutes=15)
    assert parse_duration("1h") == timedelta(hours=1)
    assert parse_duration("1d") == timedelta(days=1)
    assert parse_duration("7d") == timedelta(days=7)
    assert parse_duration("30s") == timedelta(seconds=30)


@pytest.mark.parametrize("bad", ["", "1w", "h", "1.5h", "0h", "-1d", "1 hour"])
def test_parse_duration_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


# ----------------------------------------------------------- overdue math


def test_not_overdue_spawns_nothing(tmp_path):
    recurring, beads = _store(tmp_path, **_weekly(last_spawned=T0.isoformat()))
    spawned = reconcile(recurring, beads, now=T0 + timedelta(days=3))
    assert spawned == []
    assert beads.list() == []


def test_overdue_spawns_exactly_one(tmp_path):
    recurring, beads = _store(tmp_path, **_weekly(last_spawned=T0.isoformat()))
    spawned = reconcile(recurring, beads, now=T0 + timedelta(days=7, hours=1))
    assert len(spawned) == 1
    task = beads.list()[0]
    assert task.metadata["spawned_by"] == "rec-001"
    assert task.assigned_agent == "analyst"
    assert task.source == "recurring"


def test_never_spawned_is_overdue(tmp_path):
    recurring, beads = _store(tmp_path, **_weekly(last_spawned=None))
    spawned = reconcile(recurring, beads, now=T0)
    assert len(spawned) == 1


def test_one_bead_per_interval_of_continuous_uptime(tmp_path):
    """Acceptance 1: every:1h spawns exactly one bead per hour, frozen clock."""
    recurring, beads = _store(
        tmp_path, **_weekly(last_spawned=T0.isoformat(), schedule={"every": "1h"})
    )
    for hour in range(1, 4):
        now = T0 + timedelta(hours=hour, minutes=1)
        reconcile(recurring, beads, now=now)
        # Simulate the work completing so the dedup guard doesn't hold.
        for t in beads.ready():
            beads.complete(t.id)
    assert len(beads.list()) == 3


def test_disabled_def_never_fires(tmp_path):
    recurring, beads = _store(
        tmp_path, **_weekly(last_spawned=None, enabled=False)
    )
    assert reconcile(recurring, beads, now=T0) == []


# ------------------------------------------------------------- catch up


def test_catch_up_latest_spawns_one_after_downtime(tmp_path, capsys):
    """Acceptance 2: 3 days down -> one bead (latest) + WARNING logging the gap."""
    recurring, beads = _store(
        tmp_path,
        **_weekly(last_spawned=T0.isoformat(), schedule={"every": "1d"}),
    )
    spawned = reconcile(recurring, beads, now=T0 + timedelta(days=3, hours=2))
    out = capsys.readouterr().out
    assert len(spawned) == 1
    assert "WARNING" in out
    assert "missed 3 interval(s)" in out


def test_catch_up_all_spawns_one_per_missed_interval(tmp_path):
    recurring, beads = _store(
        tmp_path,
        **_weekly(
            last_spawned=T0.isoformat(),
            schedule={"every": "1d"},
            catch_up="all",
        ),
    )
    spawned = reconcile(recurring, beads, now=T0 + timedelta(days=3, hours=2))
    assert len(spawned) == 3


def test_catch_up_advance_is_phase_preserving(tmp_path):
    """After catch-up, last_spawned lands on the schedule grid, not wall clock."""
    recurring, beads = _store(
        tmp_path,
        **_weekly(last_spawned=T0.isoformat(), schedule={"every": "1d"}),
    )
    reconcile(recurring, beads, now=T0 + timedelta(days=3, hours=2))
    d = recurring.get("rec-001")
    assert datetime.fromisoformat(d.last_spawned) == T0 + timedelta(days=3)


# ---------------------------------------------------------------- dedup


def test_open_spawned_bead_blocks_new_spawn(tmp_path):
    recurring, beads = _store(
        tmp_path, **_weekly(last_spawned=T0.isoformat(), schedule={"every": "1h"})
    )
    first = reconcile(recurring, beads, now=T0 + timedelta(hours=1, minutes=1))
    assert len(first) == 1
    # Two hours later the first bead is still open: no pile-up.
    second = reconcile(recurring, beads, now=T0 + timedelta(hours=3))
    assert second == []
    assert len(beads.list()) == 1


def test_completed_bead_allows_next_spawn(tmp_path):
    recurring, beads = _store(
        tmp_path, **_weekly(last_spawned=T0.isoformat(), schedule={"every": "1h"})
    )
    first = reconcile(recurring, beads, now=T0 + timedelta(hours=1, minutes=1))
    beads.complete(first[0].id)
    second = reconcile(recurring, beads, now=T0 + timedelta(hours=2, minutes=2))
    assert len(second) == 1


def test_crash_window_duplicates_never_skips(tmp_path):
    """Bead written but last_spawned NOT updated (crash window): once the
    open bead closes, the next reconcile spawns again — duplicate, never skip."""
    recurring, beads = _store(
        tmp_path, **_weekly(last_spawned=T0.isoformat(), schedule={"every": "1h"})
    )
    now = T0 + timedelta(hours=1, minutes=1)
    # Simulate the crash window: bead exists, definition still has old last_spawned.
    crashed = beads.create(
        title="Weekly KPI report",
        description="x",
        metadata={"spawned_by": "rec-001"},
    )
    # While open: dedup guard holds, last_spawned must NOT advance.
    assert reconcile(recurring, beads, now=now) == []
    assert recurring.get("rec-001").last_spawned == T0.isoformat()
    # After it closes: still overdue -> spawns (the duplicate), never skips.
    beads.complete(crashed.id)
    spawned = reconcile(recurring, beads, now=now)
    assert len(spawned) == 1


# ------------------------------------------------------------ quarantine


def test_malformed_lines_quarantined_and_preserved(tmp_path, capsys):
    path = tmp_path / "recurring.jsonl"
    good = RecurringDef(id="rec-ok", title="ok", schedule={"every": "1d"})
    bad_lines = [
        "{not json",
        '{"id": "rec-bad-schedule", "title": "x", "schedule": {"every": "1w"}}',
        '{"id": "rec-bad-catchup", "title": "x", "schedule": {"every": "1d"}, "catch_up": "sometimes"}',
    ]
    path.write_text(good.to_json() + "\n" + "\n".join(bad_lines) + "\n")

    store = Recurring(path)
    defs = store.list()
    out = capsys.readouterr().out

    assert [d.id for d in defs] == ["rec-ok"]
    assert out.count("quarantined") == 3

    # Quarantined lines survive a rewrite verbatim.
    store.update("rec-ok", title="ok2")
    content = path.read_text()
    for raw in bad_lines:
        assert raw in content


def test_unknown_fields_ignored_forward_compat(tmp_path):
    path = tmp_path / "recurring.jsonl"
    path.write_text(
        '{"id": "rec-1", "title": "t", "schedule": {"every": "1d"}, "future_field": 42}\n'
    )
    defs = Recurring(path).list()
    assert len(defs) == 1 and defs[0].id == "rec-1"


# ------------------------------------------------- superseding stale failures


_SAMPLE_SEQ = itertools.count()


def _sample(beads: Beads, def_id: str, status: TaskStatus, title="Verify child"):
    """One recurring sample, written the way reconcile() writes them.

    The docstring's claim used to be false: this wrote a CONSTANT
    ``source_id`` (``f"{def_id}:x"``) for every sample, while reconcile()
    writes ``f"{d.id}:{now.isoformat()}:{i}"`` — distinct per spawn, because
    two runs of one schedule are two different pieces of work. The natural-key
    index made the divergence visible (the second sample deduped into the
    first). Fixed by making the helper actually faithful; the sequence counter
    stands in for reconcile()'s wall-clock component.
    """
    t = beads.create(
        title=title,
        description=title,
        source="recurring",
        source_id=f"{def_id}:{next(_SAMPLE_SEQ)}",
        metadata={"spawned_by": def_id},
    )
    if status is TaskStatus.DONE:
        beads.complete(t.id)
    elif status is TaskStatus.FAILED:
        beads.fail(t.id)
    return beads.get(t.id)


def test_failure_older_than_a_later_success_is_superseded(tmp_path):
    _, beads = _store(tmp_path)
    bad = _sample(beads, "verify-x", TaskStatus.FAILED)
    _sample(beads, "verify-x", TaskStatus.DONE)

    closed = supersede_stale_failures(beads)

    assert [c.id for c in closed] == [bad.id]
    assert beads.get(bad.id).status is TaskStatus.DONE


def test_the_most_recent_failure_is_never_superseded(tmp_path):
    # If the LAST sample failed, that is live news — the check is broken right now.
    _, beads = _store(tmp_path)
    _sample(beads, "verify-x", TaskStatus.DONE)
    still_broken = _sample(beads, "verify-x", TaskStatus.FAILED)

    assert supersede_stale_failures(beads) == []
    assert beads.get(still_broken.id).status is TaskStatus.FAILED


def test_a_definition_that_never_passed_keeps_every_failure(tmp_path):
    _, beads = _store(tmp_path)
    a = _sample(beads, "verify-x", TaskStatus.FAILED)
    b = _sample(beads, "verify-x", TaskStatus.FAILED)

    assert supersede_stale_failures(beads) == []
    assert {beads.get(a.id).status, beads.get(b.id).status} == {TaskStatus.FAILED}


def test_definitions_do_not_supersede_each_other(tmp_path):
    # y passing says nothing about whether x is healthy.
    _, beads = _store(tmp_path)
    x_bad = _sample(beads, "verify-x", TaskStatus.FAILED)
    _sample(beads, "verify-y", TaskStatus.DONE)

    assert supersede_stale_failures(beads) == []
    assert beads.get(x_bad.id).status is TaskStatus.FAILED


def test_non_recurring_failures_are_never_touched(tmp_path):
    # Real work that failed is not a sample — it must survive untouched.
    _, beads = _store(tmp_path)
    manual = beads.create(title="Ship the thing", description="real work")
    beads.fail(manual.id)
    _sample(beads, "verify-x", TaskStatus.DONE)

    assert supersede_stale_failures(beads) == []
    assert beads.get(manual.id).status is TaskStatus.FAILED


def test_superseded_result_records_why(tmp_path):
    _, beads = _store(tmp_path)
    bad = _sample(beads, "verify-x", TaskStatus.FAILED)
    _sample(beads, "verify-x", TaskStatus.DONE)

    supersede_stale_failures(beads)

    assert "Superseded" in (beads.get(bad.id).result or "")


def test_superseding_is_idempotent(tmp_path):
    _, beads = _store(tmp_path)
    _sample(beads, "verify-x", TaskStatus.FAILED)
    _sample(beads, "verify-x", TaskStatus.DONE)

    assert len(supersede_stale_failures(beads)) == 1
    assert supersede_stale_failures(beads) == []


def test_feed_ingest_failures_supersede_on_the_same_feed_source(tmp_path):
    # Feeds key on metadata.feed_source_id, not spawned_by — same series semantics.
    _, beads = _store(tmp_path)
    bad = beads.create(title="Ingest youtube: @x", description="d", source="feeds",
                       metadata={"feed_source_id": "f1", "feed_kind": "ingest"})
    beads.fail(bad.id)
    ok = beads.create(title="Ingest youtube: @x", description="d", source="feeds",
                      metadata={"feed_source_id": "f1", "feed_kind": "ingest"})
    beads.complete(ok.id)

    assert [c.id for c in supersede_stale_failures(beads)] == [bad.id]


def test_a_different_feed_source_does_not_supersede(tmp_path):
    _, beads = _store(tmp_path)
    bad = beads.create(title="Ingest youtube: @x", description="d", source="feeds",
                       metadata={"feed_source_id": "f1", "feed_kind": "ingest"})
    beads.fail(bad.id)
    ok = beads.create(title="Ingest youtube: @y", description="d", source="feeds",
                      metadata={"feed_source_id": "f2", "feed_kind": "ingest"})
    beads.complete(ok.id)

    assert supersede_stale_failures(beads) == []
    assert beads.get(bad.id).status is TaskStatus.FAILED


def test_a_feeds_bead_without_a_source_id_is_treated_as_real_work(tmp_path):
    # No feed_source_id means we cannot prove it is a sample — leave it alone.
    _, beads = _store(tmp_path)
    manual = beads.create(title="Fix the feed pipeline", description="d", source="feeds")
    beads.fail(manual.id)
    ok = beads.create(title="Ingest x", description="d", source="feeds",
                      metadata={"feed_source_id": "f1"})
    beads.complete(ok.id)

    assert supersede_stale_failures(beads) == []
    assert beads.get(manual.id).status is TaskStatus.FAILED


# ---------------------------------------------------------- resolved RCA beads


def _rca(beads: Beads, subject_id: str, error="boom"):
    t = beads.create(title="[RCA] something", description="d", source="rca",
                     source_id=f"rca-for:{subject_id}:cycle1",
                     metadata={"rca_for": subject_id, "rca_error": error})
    beads.fail(t.id)
    return beads.get(t.id)


def test_rca_closes_when_its_subject_is_no_longer_failing(tmp_path):
    _, beads = _store(tmp_path)
    subject = beads.create(title="Ingest x", description="d")
    beads.complete(subject.id)
    rca = _rca(beads, subject.id)

    assert [c.id for c in supersede_resolved_rcas(beads)] == [rca.id]
    assert beads.get(rca.id).status is TaskStatus.DONE


def test_rca_stays_open_while_its_subject_is_still_failing(tmp_path):
    _, beads = _store(tmp_path)
    subject = beads.create(title="Ingest x", description="d")
    beads.fail(subject.id)
    rca = _rca(beads, subject.id)

    assert supersede_resolved_rcas(beads) == []
    assert beads.get(rca.id).status is TaskStatus.FAILED


def test_rca_with_a_dangling_subject_is_left_alone(tmp_path):
    # A missing subject is a bug worth surfacing, not something to quietly close.
    _, beads = _store(tmp_path)
    rca = _rca(beads, "ac-doesnotexist")

    assert supersede_resolved_rcas(beads) == []
    assert beads.get(rca.id).status is TaskStatus.FAILED


def test_rca_result_preserves_the_original_error(tmp_path):
    _, beads = _store(tmp_path)
    subject = beads.create(title="Ingest x", description="d")
    beads.complete(subject.id)
    rca = _rca(beads, subject.id, error="ZAI_API_KEY not found")

    supersede_resolved_rcas(beads)

    assert "ZAI_API_KEY not found" in (beads.get(rca.id).result or "")


def test_a_non_rca_failure_is_never_closed_by_this_sweep(tmp_path):
    _, beads = _store(tmp_path)
    subject = beads.create(title="Ingest x", description="d")
    beads.complete(subject.id)
    real = beads.create(title="Real work", description="d",
                        metadata={"rca_for": subject.id})  # metadata alone is not enough
    beads.fail(real.id)

    assert supersede_resolved_rcas(beads) == []
    assert beads.get(real.id).status is TaskStatus.FAILED
