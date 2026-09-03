"""The parent's memory of its OWN outage (ac-af68be91).

`_downgrade_for_host_outage` (ac-c5020eb8) reads the parent's *current*
lateness, which only exists on the parent's first cycle back. launchd
coalesces every StartInterval job it missed into one wake but not into one
instant, so children come back later than the parent — and on the parent's
second cycle it is punctual again while they are still stale.

Replayed here from the real 2026-08-31 timeline on bigmac:

    2026-08-30 21:59:33Z  parent's last pre-outage cycle
    2026-08-30 22:01:51Z  ache's last pre-outage cycle
    ...                   host stops ticking (~9h)
    2026-08-31 07:00:08Z  parent's first cycle back   -> firing #1
    2026-08-31 07:46:45Z  ache/m3bl/semijoias back    -> firing #2 landed here
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from agentco_harness.children import verify_child, ChildRef
from agentco_harness.config import AgentConfig, BackoffConfig, Config, LLMConfig
from agentco_harness.orchestrator import Orchestrator

HOUR = 3600.0
PRE_OUTAGE = datetime(2026, 8, 30, 21, 59, 33, tzinfo=timezone.utc)
RECOVERED = datetime(2026, 8, 31, 7, 0, 8, tzinfo=timezone.utc)
CHILDREN_BACK = datetime(2026, 8, 31, 7, 46, 45, tzinfo=timezone.utc)


def _orch(tmp_path) -> Orchestrator:
    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    config.llm = LLMConfig(default_provider="lmstudio", default_model="local-model")
    config.agents = {"pm": AgentConfig(model="local-model")}
    config.notify.enabled = False
    config.triage.model = "none"
    config.backoff = BackoffConfig()
    return Orchestrator(config)


def _write_hb(tmp_path, completed: datetime, **extra) -> None:
    payload = {
        "instance": "portfolio",
        "cycle_completed_at": completed.isoformat(),
        "current_interval_s": HOUR,
        "next_due_at": (completed + timedelta(seconds=HOUR)).isoformat(),
    }
    payload.update(extra)
    (tmp_path / "heartbeat.json").write_text(json.dumps(payload))


def _child(tmp_path, last_cycle: datetime) -> ChildRef:
    d = tmp_path / "child"
    d.mkdir()
    (d / "heartbeat.json").write_text(
        json.dumps(
            {
                "instance": "ache",
                "cycle_completed_at": last_cycle.isoformat(),
                "current_interval_s": HOUR,
                "next_due_at": (last_cycle + timedelta(seconds=HOUR)).isoformat(),
            }
        )
    )
    return ChildRef(name="ache", path=str(d), expected_interval="1h")


def test_outage_is_recorded_on_the_first_cycle_back(tmp_path):
    """A 9h gap between two cycles is written into the heartbeat as evidence."""
    _write_hb(tmp_path, PRE_OUTAGE)
    orch = _orch(tmp_path)

    evidence = orch._outage_evidence(completed=RECOVERED, interval_s=HOUR)

    assert evidence["last_outage_gap_s"] == (RECOVERED - PRE_OUTAGE).total_seconds()
    assert evidence["last_outage_ended_at"] == RECOVERED.isoformat()


def test_a_normal_cycle_records_no_outage(tmp_path):
    """An on-cadence node writes no outage fields at all."""
    _write_hb(tmp_path, RECOVERED)
    orch = _orch(tmp_path)

    assert orch._outage_evidence(
        completed=RECOVERED + timedelta(seconds=HOUR), interval_s=HOUR
    ) == {}
    assert orch._own_recent_outage_seconds(RECOVERED + timedelta(seconds=HOUR)) == 0.0


def test_outage_evidence_is_carried_forward_then_expires(tmp_path):
    """Admissible for OUTAGE_EVIDENCE_WINDOW_INTERVALS, then gone."""
    gap = (RECOVERED - PRE_OUTAGE).total_seconds()
    _write_hb(
        tmp_path,
        RECOVERED,
        last_outage_gap_s=gap,
        last_outage_ended_at=RECOVERED.isoformat(),
    )
    orch = _orch(tmp_path)

    carried = orch._outage_evidence(completed=CHILDREN_BACK, interval_s=HOUR)
    assert carried["last_outage_gap_s"] == gap
    assert carried["last_outage_ended_at"] == RECOVERED.isoformat()

    # Past the window the marker simply stops being written. The previous
    # heartbeat has to be on-cadence here, or the gap itself is a fresh outage.
    _write_hb(
        tmp_path,
        RECOVERED + timedelta(hours=2, minutes=30),
        last_outage_gap_s=gap,
        last_outage_ended_at=RECOVERED.isoformat(),
    )
    expired = orch._outage_evidence(
        completed=RECOVERED + timedelta(hours=3), interval_s=HOUR
    )
    assert expired == {}


def test_own_recent_outage_seconds_reads_the_window(tmp_path):
    gap = (RECOVERED - PRE_OUTAGE).total_seconds()
    _write_hb(
        tmp_path,
        RECOVERED,
        last_outage_gap_s=gap,
        last_outage_ended_at=RECOVERED.isoformat(),
    )
    orch = _orch(tmp_path)

    assert orch._own_recent_outage_seconds(CHILDREN_BACK) == gap
    assert orch._own_recent_outage_seconds(RECOVERED + timedelta(hours=3)) == 0.0


def test_the_real_firing_no_longer_fails(tmp_path):
    """End-to-end replay of ac-ec237455 — the bead this fix exists to stop.

    Parent punctual again (next_due 08:00:08Z, now 07:46:45Z), child still
    carrying the outage. Old behaviour: fail. New: warn, and it self-clears
    when ache's own cycle lands seconds later.
    """
    gap = (RECOVERED - PRE_OUTAGE).total_seconds()
    _write_hb(
        tmp_path,
        RECOVERED,
        last_outage_gap_s=gap,
        last_outage_ended_at=RECOVERED.isoformat(),
    )
    orch = _orch(tmp_path)
    child = _child(tmp_path, datetime(2026, 8, 30, 22, 1, 51, tzinfo=timezone.utc))

    # The parent is NOT late — this is precisely why the first fix missed it.
    assert orch._own_next_due_at() > CHILDREN_BACK

    before = verify_child(child, now=CHILDREN_BACK, parent_next_due_at=orch._own_next_due_at())
    assert before["level"] == "fail"

    after = verify_child(
        child,
        now=CHILDREN_BACK,
        parent_next_due_at=orch._own_next_due_at(),
        parent_recent_outage_s=orch._own_recent_outage_seconds(CHILDREN_BACK),
    )
    assert after["level"] == "warn"
    assert after["ok"] is True
    assert "host-level outage" in after["detail"]
