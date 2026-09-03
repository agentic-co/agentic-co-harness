"""Delegation layer, Stage 1 — people as first-class executors.

Covers the schema (assigned_to round-trip + old-line parse), the queue-layer
exclusion, the defense-in-depth dispatch guard, the `me` surfacing, the
human-lineage invariant, the humans.enabled kill-switch, and snooze visibility.
Telegram command flows live in test_server.py. All offline against fakes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from agentco_harness.beads import Beads, HumanLineageError, Task, TaskStatus
from agentco_harness.cli import main
from agentco_harness.config import AgentConfig, Config, LLMConfig
from agentco_harness.humans import decline_task, snooze_task, TaskStateError
from agentco_harness.me import ranked
from agentco_harness.orchestrator import Orchestrator

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)


# ----------------------------------------------------------------- helpers


def _build_config(tmp_path) -> Config:
    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    config.llm = LLMConfig(default_provider="lmstudio", default_model="local-model")
    config.agents = {"pm": AgentConfig(model="local-model")}
    config.notify.enabled = False
    return config


def _write_config(root, extra: str = "") -> str:
    root.mkdir(parents=True, exist_ok=True)
    cfg = root / "config.yaml"
    cfg.write_text("tasks_path: tasks.jsonl\n" + extra)
    return str(cfg)


# --------------------------------------------------------------- schema


def test_assigned_to_round_trips():
    t = Task(id="ac-1", title="call the accountant", description="x", assigned_to="human:mabidoli")
    line = t.to_json()
    assert json.loads(line)["assigned_to"] == "human:mabidoli"
    back = Task.from_json(line)
    assert back.assigned_to == "human:mabidoli"


def test_old_line_without_assigned_to_parses():
    # A pre-delegation JSONL line has no assigned_to key at all.
    old = json.dumps(
        {
            "id": "ac-old",
            "title": "legacy",
            "description": "x",
            "status": "pending",
            "priority": 2,
        }
    )
    task = Task.from_json(old)
    assert task.assigned_to is None  # additive field defaults, never raises


def test_assigned_to_persists_through_beads(tmp_path):
    beads = Beads(str(tmp_path / "tasks.jsonl"))
    t = beads.create("desk work", "x")
    beads.update(t.id, assigned_to="human:mabidoli")
    refreshed = beads.get(t.id)
    assert refreshed.assigned_to == "human:mabidoli"
    # And it survives a re-read from disk (fresh Beads instance).
    assert Beads(str(tmp_path / "tasks.jsonl")).get(t.id).assigned_to == "human:mabidoli"


# --------------------------------------------------------------- ready()


def test_ready_excludes_human_assigned(tmp_path):
    beads = Beads(str(tmp_path / "tasks.jsonl"))
    agent_task = beads.create("agent work", "x")
    human_task = beads.create("human work", "x")
    beads.update(human_task.id, assigned_to="human:mabidoli")

    ready_ids = {t.id for t in beads.ready()}
    assert agent_task.id in ready_ids
    assert human_task.id not in ready_ids  # never enters dispatch


def test_ready_excludes_any_non_none_assignee(tmp_path):
    # Not just human: — ANY assignee token keeps a task out of the ready set.
    beads = Beads(str(tmp_path / "tasks.jsonl"))
    t = beads.create("mystery", "x")
    beads.update(t.id, assigned_to="robot:x")
    assert t.id not in {r.id for r in beads.ready()}


# ---------------------------------------------- zero done/errors over cycles


def test_pending_human_task_contributes_zero_over_two_cycles(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch = Orchestrator(_build_config(tmp_path))
    # triage must never be consulted (queue is empty of ready work); make it
    # loud if it is, so a regression that dispatches the human task is caught.
    monkeypatch.setattr(
        orch, "_make_triage_lm", lambda: (_ for _ in ()).throw(RuntimeError("LM down"))
    )

    t = orch.beads.create("call the bank", "x")
    orch.beads.update(t.id, assigned_to="human:mabidoli")

    for _ in range(2):
        summary = orch.cycle(now=NOW)
        assert summary["executed"] == 0
        assert summary["errors"] == 0

    # The task is untouched — still pending, still human-owned.
    after = orch.beads.get(t.id)
    assert after.status == TaskStatus.PENDING
    assert after.assigned_to == "human:mabidoli"


# ------------------------------------------------------- dispatch guard


def test_dispatch_guard_blocks_human_task(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    orch = Orchestrator(_build_config(tmp_path))
    t = orch.beads.create("human only", "x")
    orch.beads.update(t.id, assigned_to="human:mabidoli")

    # Even if it somehow reaches dispatch, it must NOT execute with any LLM.
    ok = orch._execute_cycle_task(orch.beads.get(t.id), now=NOW)
    assert ok is False
    blocked = orch.beads.get(t.id)
    assert blocked.status == TaskStatus.BLOCKED
    out = capsys.readouterr().out
    assert "BLOCKED" in out
    assert "human executor" in out


def test_dispatch_guard_blocks_unknown_scheme(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    orch = Orchestrator(_build_config(tmp_path))
    t = orch.beads.create("weird", "x")
    orch.beads.update(t.id, assigned_to="robot:optimus")

    ok = orch._execute_cycle_task(orch.beads.get(t.id), now=NOW)
    assert ok is False
    assert orch.beads.get(t.id).status == TaskStatus.BLOCKED
    assert "unrecognized assignee token" in capsys.readouterr().out


# --------------------------------------------------------------- me surface


def test_me_lists_human_assigned_ranked(tmp_path):
    cfg = _write_config(tmp_path / "co")
    beads = Beads(str(tmp_path / "co" / "tasks.jsonl"))
    t = beads.create("review the lease", "x")
    beads.update(t.id, assigned_to="human:mabidoli")

    items = ranked(cfg, now=NOW)
    human = [i for i in items if i.kind == "human_assigned"]
    assert len(human) == 1
    assert human[0].task_id == t.id
    assert "mabidoli" in human[0].detail
    assert "tasks complete" in human[0].resolve and t.id in human[0].resolve


def test_human_assigned_outranks_plain_blocked(tmp_path):
    # weight(human_assigned)=3.5 > weight(blocked)=2.0 at equal company/age.
    cfg = _write_config(tmp_path / "co")
    beads = Beads(str(tmp_path / "co" / "tasks.jsonl"))
    h = beads.create("human task", "x")
    beads.update(h.id, assigned_to="human:mabidoli")
    gate = beads.create("gate", "x")
    beads.create("waits on gate", "x", blocked_by=[gate.id])

    items = ranked(cfg, now=NOW)
    kinds = [i.kind for i in items]
    assert kinds.index("human_assigned") < kinds.index("blocked")


# ------------------------------------------------------ human-lineage invariant


def test_human_lineage_invariant_raises_on_clear(tmp_path):
    beads = Beads(str(tmp_path / "tasks.jsonl"))
    t = beads.create("owned", "x")
    beads.update(t.id, assigned_to="human:mabidoli")
    with pytest.raises(HumanLineageError):
        beads.update(t.id, assigned_to=None)  # human → None: forbidden


def test_human_lineage_invariant_raises_on_agent_flip(tmp_path):
    beads = Beads(str(tmp_path / "tasks.jsonl"))
    t = beads.create("owned", "x")
    beads.update(t.id, assigned_to="human:mabidoli")
    with pytest.raises(HumanLineageError):
        beads.update(t.id, assigned_to="agent:dev")  # human → agent: forbidden


def test_human_lineage_allows_explicit_reassign_flag(tmp_path):
    beads = Beads(str(tmp_path / "tasks.jsonl"))
    t = beads.create("owned", "x")
    beads.update(t.id, assigned_to="human:mabidoli")
    # The one sanctioned path: explicit approval clears it.
    updated = beads.update(t.id, assigned_to=None, allow_human_reassign=True)
    assert updated.assigned_to is None


def test_human_lineage_allows_reassign_to_another_human(tmp_path):
    beads = Beads(str(tmp_path / "tasks.jsonl"))
    t = beads.create("owned", "x")
    beads.update(t.id, assigned_to="human:alice")
    updated = beads.update(t.id, assigned_to="human:bob")  # human → human: allowed
    assert updated.assigned_to == "human:bob"


# ----------------------------------------------------------- snooze visibility


def test_snoozed_task_absent_from_me_until_expiry(tmp_path):
    cfg = _write_config(tmp_path / "co")
    beads = Beads(str(tmp_path / "co" / "tasks.jsonl"))
    t = beads.create("annual review", "x")
    beads.update(t.id, assigned_to="human:mabidoli")
    snooze_task(beads, t.id, "2d", now=NOW)

    # During the snooze window: hidden.
    during = ranked(cfg, now=NOW + timedelta(days=1))
    assert not any(i.task_id == t.id for i in during)

    # After it elapses: visible again.
    after = ranked(cfg, now=NOW + timedelta(days=3))
    assert any(i.task_id == t.id and i.kind == "human_assigned" for i in after)


def test_decline_returns_task_to_queue(tmp_path):
    beads = Beads(str(tmp_path / "tasks.jsonl"))
    t = beads.create("do taxes", "x")
    beads.update(t.id, assigned_to="human:mabidoli")

    declined = decline_task(beads, t.id, reason="not my area")
    assert declined.assigned_to is None
    assert declined.status == TaskStatus.PENDING
    assert declined.metadata["decline_history"][-1]["reason"] == "not my area"
    # Back in the ready set now that it is unassigned.
    assert t.id in {r.id for r in beads.ready()}


# ----------------------------------------------------------------- CLI


def test_cli_create_assign_and_task_class(tmp_path):
    cfg = _write_config(tmp_path / "co")
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--config", cfg, "tasks", "create", "Review lease",
            "--assign", "human:mabidoli", "--task-class", "personal",
        ],
    )
    assert result.exit_code == 0, result.output

    beads = Beads(str(tmp_path / "co" / "tasks.jsonl"))
    created = beads.list()[0]
    assert created.assigned_to == "human:mabidoli"
    assert created.metadata["task_class"] == "personal"


def test_cli_create_rejects_unknown_assignee(tmp_path):
    cfg = _write_config(tmp_path / "co")
    runner = CliRunner()
    result = runner.invoke(
        main, ["--config", cfg, "tasks", "create", "x", "--assign", "robot:x"]
    )
    assert result.exit_code != 0
    assert "Unrecognized assignee" in result.output


def test_cli_create_refuses_when_humans_disabled(tmp_path):
    cfg = _write_config(tmp_path / "co", extra="humans:\n  enabled: false\n")
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--config", cfg, "tasks", "create", "x", "--assign", "human:mabidoli"],
    )
    assert result.exit_code != 0
    assert "humans.enabled is false" in result.output
    # And nothing was created.
    assert Beads(str(tmp_path / "co" / "tasks.jsonl")).list() == []


def test_cli_decline_and_snooze(tmp_path):
    cfg = _write_config(tmp_path / "co")
    beads = Beads(str(tmp_path / "co" / "tasks.jsonl"))
    t = beads.create("chore", "x")
    beads.update(t.id, assigned_to="human:mabidoli")

    runner = CliRunner()
    r1 = runner.invoke(
        main, ["--config", cfg, "tasks", "decline", t.id, "--reason", "busy"]
    )
    assert r1.exit_code == 0, r1.output
    assert beads.get(t.id).assigned_to is None

    # Re-assign then snooze.
    beads.update(t.id, assigned_to="human:mabidoli", allow_human_reassign=True)
    r2 = runner.invoke(main, ["--config", cfg, "tasks", "snooze", t.id, "--for", "2d"])
    assert r2.exit_code == 0, r2.output
    assert beads.get(t.id).metadata.get("snoozed_until")


def test_cli_snooze_rejects_bad_interval(tmp_path):
    cfg = _write_config(tmp_path / "co")
    beads = Beads(str(tmp_path / "co" / "tasks.jsonl"))
    t = beads.create("chore", "x")
    runner = CliRunner()
    result = runner.invoke(main, ["--config", cfg, "tasks", "snooze", t.id, "--for", "banana"])
    assert result.exit_code != 0
    assert "Invalid --for interval" in result.output


# --------------------------------------------------------------- config


def test_humans_config_defaults_enabled():
    assert Config().humans.enabled is True


def test_humans_config_parsed_from_yaml(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("humans:\n  enabled: false\n")
    assert Config.load(cfg).humans.enabled is False


def test_humans_disabled_round_trips_through_save(tmp_path):
    config = Config()
    config.humans.enabled = False
    cfg = tmp_path / "config.yaml"
    config.save(cfg)
    assert Config.load(cfg).humans.enabled is False


def test_humans_unknown_key_warns(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("humans:\n  enabled: true\n  mystery: 1\n")
    Config.load(cfg)
    out = capsys.readouterr().out
    assert "humans" in out and "mystery" in out


# ---------------------------------------- HIGH-3: done/decline/snooze guards


def test_decline_refuses_non_human_task(tmp_path):
    beads = Beads(str(tmp_path / "tasks.jsonl"))
    t = beads.create("agent job", "x", assigned_agent="claude")  # not human-owned
    with pytest.raises(TaskStateError):
        decline_task(beads, t.id, reason="nope")
    # Untouched — never returned to the queue on a bad decline.
    assert beads.get(t.id).status == TaskStatus.PENDING


def test_decline_refuses_wrong_status(tmp_path):
    # A human-assigned task in PENDING_APPROVAL is not open work — declining it
    # would resurrect/return gated work.
    beads = Beads(str(tmp_path / "tasks.jsonl"))
    t = beads.create(
        "gated", "x", assigned_to="human:mabidoli", status=TaskStatus.PENDING_APPROVAL
    )
    with pytest.raises(TaskStateError):
        decline_task(beads, t.id)
    assert beads.get(t.id).status == TaskStatus.PENDING_APPROVAL


def test_decline_refuses_done_task(tmp_path):
    # Never resurrect DONE work via decline.
    beads = Beads(str(tmp_path / "tasks.jsonl"))
    t = beads.create("finished", "x", assigned_to="human:mabidoli")
    beads.complete(t.id)
    with pytest.raises(TaskStateError):
        decline_task(beads, t.id)
    assert beads.get(t.id).status == TaskStatus.DONE


def test_snooze_refuses_non_human_task(tmp_path):
    beads = Beads(str(tmp_path / "tasks.jsonl"))
    t = beads.create("agent job", "x", assigned_agent="claude")
    with pytest.raises(TaskStateError):
        snooze_task(beads, t.id, "2d", now=NOW)
    assert not beads.get(t.id).metadata.get("snoozed_until")


def test_snooze_allows_failed_human_task(tmp_path):
    # snooze carries only the ownership guard (not the pending/blocked status
    # guard) — a human triaging their own FAILED item may defer it.
    beads = Beads(str(tmp_path / "tasks.jsonl"))
    t = beads.create("broke", "x", assigned_to="human:mabidoli")
    beads.fail(t.id, result="boom")
    snoozed = snooze_task(beads, t.id, "2d", now=NOW)
    assert snoozed.metadata.get("snoozed_until")


def test_cli_decline_refuses_non_human(tmp_path):
    cfg = _write_config(tmp_path / "co")
    beads = Beads(str(tmp_path / "co" / "tasks.jsonl"))
    t = beads.create("agent job", "x", assigned_agent="claude")
    runner = CliRunner()
    result = runner.invoke(main, ["--config", cfg, "tasks", "decline", t.id])
    assert result.exit_code != 0
    assert "Cannot decline" in result.output
    assert "not human-assigned" in result.output


# ---------------------------------------- MEDIUM-8: snooze honored in every me branch


def test_snoozed_failed_human_task_hidden_from_me(tmp_path):
    """A FAILED human task flows through me's `failed` branch, NOT the
    human_assigned branch — snoozing it must still hide it (MEDIUM-8: the snooze
    check is honored in every collection branch, not just human_assigned)."""
    cfg = _write_config(tmp_path / "co")
    beads = Beads(str(tmp_path / "co" / "tasks.jsonl"))
    t = beads.create("broke", "x", assigned_to="human:mabidoli")
    beads.fail(t.id, result="boom")
    # Sanity: before snoozing it shows up as a failed item.
    before = ranked(cfg, now=NOW)
    assert any(i.task_id == t.id and i.kind == "failed" for i in before)

    snooze_task(beads, t.id, "2d", now=NOW)
    during = ranked(cfg, now=NOW + timedelta(days=1))
    assert not any(i.task_id == t.id for i in during)  # hidden across the branch
    after = ranked(cfg, now=NOW + timedelta(days=3))
    assert any(i.task_id == t.id for i in after)  # visible again after expiry
