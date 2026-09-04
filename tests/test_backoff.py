"""Adaptive cycle backoff (v0.4.1).

launchd stays fixed-interval; the backoff gate inside `cycle` decides whether a
wake runs or exits fast. Invariants under test:

  * an idle cycle doubles the interval by `factor` up to `max`;
  * any live activity (open bead, new bead, due recurring def, --force) resets
    the interval to baseline AND runs the cycle now;
  * a skipped wake exits fast — it touches `last_wake_at` but NEVER moves the
    heartbeat's `cycle_completed_at`;
  * the heartbeat carries `current_interval_s` + `next_due_at`;
  * a malformed backoff block is a loud `doctor` FAIL and degrades to disabled;
  * an instance with backoff disabled behaves exactly as before (no skip, no
    new heartbeat fields).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import yaml

from agentco_harness.beads import TaskStatus
from agentco_harness.config import AgentConfig, BackoffConfig, Config, LLMConfig
from agentco_harness.doctor import run_doctor
from agentco_harness.executor import ExecResult
from agentco_harness.orchestrator import Orchestrator
import agentco_harness.orchestrator as orchestrator_mod

NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
HOUR = 3600.0


def _build_config(tmp_path, **backoff) -> Config:
    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    config.llm = LLMConfig(default_provider="lmstudio", default_model="local-model")
    config.agents = {"pm": AgentConfig(model="local-model")}
    config.notify.enabled = False
    config.triage.model = "none"  # skip triage quietly — not the subject here
    config.backoff = BackoffConfig(**backoff) if backoff else BackoffConfig()
    return config


def _orch(tmp_path, **backoff) -> Orchestrator:
    return Orchestrator(_build_config(tmp_path, **backoff))


def _read_hb(tmp_path) -> dict:
    return json.loads((tmp_path / "heartbeat.json").read_text())


def _fake_claude_ok(monkeypatch):
    monkeypatch.setattr(
        orchestrator_mod,
        "run_claude_task",
        lambda prompt, timeout, max_turns, model=None, cwd=None: ExecResult(
            True, '{"ok": true}', None, 0, 0.1
        ),
    )


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------- progression


def test_idle_cycles_double_interval_up_to_cap(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path, base="1h", factor=2, max="7d")

    intervals: list[float] = []
    now = NOW
    for _ in range(12):
        summary = orch.cycle(now=now)
        assert not summary.get("skipped")
        intervals.append(summary["current_interval_s"])
        now = _parse(summary["next_due_at"])  # wake exactly when next due

    # 1h → 2h → 4h → 8h … then saturates at the 7d cap, never exceeding it.
    assert intervals[0] == 2 * HOUR  # first idle cycle doubled off the 1h base
    assert intervals[1] == 4 * HOUR
    assert intervals[2] == 8 * HOUR
    cap = 7 * 86400.0
    assert intervals[-1] == cap
    assert all(i <= cap for i in intervals)
    # Monotonic non-decreasing, capped.
    assert intervals == sorted(intervals)


def test_heartbeat_carries_interval_and_due_fields(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)
    orch.cycle(now=NOW)
    hb = _read_hb(tmp_path)
    assert hb["current_interval_s"] == 2 * HOUR
    assert _parse(hb["next_due_at"]) == NOW + timedelta(hours=2)
    assert hb["cycle_completed_at"] == NOW.isoformat()


# --------------------------------------------------------------- skip gate


def test_skip_gate_exits_fast_without_moving_heartbeat(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)

    orch.cycle(now=NOW)  # idle → next_due = NOW+2h, cycle_completed_at = NOW
    hb_before = _read_hb(tmp_path)

    # A wake an hour later, still idle and before next_due → skip.
    summary = orch.cycle(now=NOW + timedelta(hours=1))
    assert summary["skipped"] is True
    assert summary["reason"] == "backoff"

    hb_after = _read_hb(tmp_path)
    # Heartbeat moves ONLY on real completion — a skip must not touch it.
    assert hb_after["cycle_completed_at"] == hb_before["cycle_completed_at"] == NOW.isoformat()

    # But the lightweight last_wake_at proves the launchd job is alive.
    state = json.loads((tmp_path / ".agentco-heartbeat.json").read_text())
    assert "last_wake_at" in state


# --------------------------------------------------------------- resets


def test_new_open_bead_resets_to_baseline_and_runs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)
    _fake_claude_ok(monkeypatch)

    orch.cycle(now=NOW)  # idle → interval 2h, next_due NOW+2h
    assert _read_hb(tmp_path)["current_interval_s"] == 2 * HOUR

    # A new bead appears. The very next wake — even before next_due — must run,
    # not skip, and snap the cadence back to baseline.
    orch.beads.create(title="ship it", description="do the thing", assigned_agent="claude")
    summary = orch.cycle(now=NOW + timedelta(hours=1))

    assert not summary.get("skipped")
    assert summary["executed"] == 1
    assert summary["current_interval_s"] == HOUR  # back to baseline


def test_blocked_bead_does_not_pin_interval_at_baseline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)

    # A bead blocked on a human (e.g. "create the Mercado Pago account") sits
    # in the queue indefinitely. It must NOT count as live activity — the
    # cycle can't act on it, so the interval should keep stretching.
    bead = orch.beads.create(title="Mercado Pago account setup", description="waiting on human")
    orch.beads.update(bead.id, status=TaskStatus.BLOCKED, result="blocked on human action")
    # Backdate creation so the 'bead created since last cycle' reset can't fire —
    # this bead has been sitting blocked since long before the cycles under test.
    tasks = orch.beads._read_all()
    for t in tasks:
        t.created_at = (NOW - timedelta(days=30)).isoformat()
    orch.beads._write_all(tasks)

    intervals: list[float] = []
    now = NOW
    for _ in range(3):
        summary = orch.cycle(now=now)
        assert not summary.get("skipped")
        intervals.append(summary["current_interval_s"])
        now = _parse(summary["next_due_at"])

    assert intervals == [2 * HOUR, 4 * HOUR, 8 * HOUR]  # doubling despite the blocked bead

    # And a wake before next_due, with only the blocked bead present, skips.
    summary = orch.cycle(now=now - timedelta(hours=1))
    assert summary.get("skipped") is True

    # But the blocked bead stays visible in the heartbeat's open count.
    assert _read_hb(tmp_path)["beads_open"] == 1


def test_force_runs_and_resets_even_before_due(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)

    orch.cycle(now=NOW)  # interval 2h
    # Idle wake before next_due WOULD skip …
    assert orch.cycle(now=NOW + timedelta(minutes=30)).get("skipped") is True
    # … but --force runs it and resets to baseline.
    forced = orch.cycle(now=NOW + timedelta(minutes=30), force=True)
    assert not forced.get("skipped")
    assert forced["current_interval_s"] == HOUR


def test_due_recurring_def_is_a_reset_signal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)
    _fake_claude_ok(monkeypatch)

    orch.cycle(now=NOW)  # interval 2h, next_due NOW+2h
    from agentco_harness.recurring import RecurringDef

    # A def due every 1h with no prior spawn is due immediately → reset.
    orch.recurring.add(
        RecurringDef(
            id="hourly", title="sync", schedule={"every": "1h"}, agent="claude",
            payload={"prompt": "x"},
        )
    )
    summary = orch.cycle(now=NOW + timedelta(minutes=30))
    assert not summary.get("skipped")
    assert summary["spawned"] == 1
    assert summary["current_interval_s"] == HOUR


# --------------------------------------------------------------- disabled / malformed


def test_disabled_backoff_behaves_like_today(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path, enabled=False)

    s1 = orch.cycle(now=NOW)
    s2 = orch.cycle(now=NOW + timedelta(minutes=1))  # would-skip window if enabled

    assert "skipped" not in s1 and "skipped" not in s2
    assert "current_interval_s" not in s1
    hb = _read_hb(tmp_path)
    assert "current_interval_s" not in hb
    assert "next_due_at" not in hb


def test_malformed_backoff_config_is_a_loud_doctor_fail(tmp_path, capsys):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {
                "tasks_path": "tasks.jsonl",
                "llm": {"default_provider": "lmstudio", "default_model": "m"},
                "backoff": {"base": "pizza", "factor": 0.5, "max": "7d"},
            }
        )
    )
    rc = run_doctor(str(cfg_file))
    out = capsys.readouterr().out
    assert rc == 1
    assert "BROKEN (backoff.config)" in out
    assert "backoff config is malformed" in out
    assert "base='pizza'" in out


def test_malformed_backoff_degrades_to_running_never_skips(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path, base="pizza")  # malformed

    s1 = orch.cycle(now=NOW)
    s2 = orch.cycle(now=NOW + timedelta(minutes=1))
    # Advisory degradation: malformed → treated as disabled, always runs.
    assert "skipped" not in s1 and "skipped" not in s2
