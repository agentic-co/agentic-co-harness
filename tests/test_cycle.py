"""Heartbeat cycle tests: atomic heartbeat.json, crash semantics, triage
fallback, verify_child and claude execution routing."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from dspy.utils.dummies import DummyLM

import agentco_harness as agentco
import agentco_harness.orchestrator as orchestrator_mod
from agentco_harness.beads import TaskStatus
from agentco_harness.children import ChildRef
from agentco_harness.config import AgentConfig, Config, LLMConfig
from agentco_harness.executor import ExecResult
from agentco_harness.orchestrator import Orchestrator
from agentco_harness.recurring import RecurringDef

NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


def _build_config(tmp_path) -> Config:
    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    config.llm = LLMConfig(default_provider="lmstudio", default_model="local-model")
    config.agents = {"pm": AgentConfig(model="local-model")}
    config.notify.enabled = False
    return config


def _orch(tmp_path) -> Orchestrator:
    return Orchestrator(_build_config(tmp_path))


def _fake_claude_ok(monkeypatch, calls: list | None = None):
    def fake(prompt, timeout, max_turns, model=None):
        if calls is not None:
            calls.append({"prompt": prompt, "timeout": timeout, "max_turns": max_turns})
        return ExecResult(True, '{"ok": true}', None, 0, 0.1)

    monkeypatch.setattr(orchestrator_mod, "run_claude_task", fake)


def _no_triage_lm(orch, monkeypatch):
    """Make triage fail fast (no network attempt) — exercises the fallback."""
    monkeypatch.setattr(
        orch, "_make_triage_lm", lambda: (_ for _ in ()).throw(RuntimeError("LM down"))
    )


# ------------------------------------------------------------- heartbeat


def test_successful_cycle_writes_heartbeat(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)
    orch.recurring.add(
        RecurringDef(
            id="rec-claude",
            title="Hourly sync",
            schedule={"every": "1h"},
            agent="claude",
            payload={"prompt": "sync things"},
        )
    )
    _fake_claude_ok(monkeypatch)
    _no_triage_lm(orch, monkeypatch)

    summary = orch.cycle(now=NOW)

    hb = json.loads((tmp_path / "heartbeat.json").read_text())
    assert hb["instance"] == tmp_path.name
    assert hb["beads_done_this_cycle"] == 1
    assert hb["recurring_spawned_this_cycle"] == 1
    assert hb["errors_this_cycle"] == 0
    assert hb["version"] == agentco.__version__
    assert datetime.fromisoformat(hb["cycle_completed_at"]) is not None
    assert summary["executed"] == 1


def test_crashed_cycle_never_writes_heartbeat(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)

    def boom():
        raise RuntimeError("queue exploded")

    monkeypatch.setattr(orch.beads, "ready", boom)
    with pytest.raises(RuntimeError):
        orch.cycle(now=NOW)
    assert not (tmp_path / "heartbeat.json").exists()


def test_bead_failure_counts_as_error_but_cycle_completes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)
    _no_triage_lm(orch, monkeypatch)
    monkeypatch.setattr(
        orchestrator_mod,
        "run_claude_task",
        lambda prompt, timeout, max_turns, model=None: ExecResult(False, "", "exited 1", 1, 0.1),
    )
    task = orch.beads.create(
        title="will fail", description="x", assigned_agent="claude"
    )

    orch.cycle(now=NOW)

    hb = json.loads((tmp_path / "heartbeat.json").read_text())
    assert hb["errors_this_cycle"] == 1
    assert orch.beads.get(task.id).status == TaskStatus.FAILED
    assert "exited 1" in orch.beads.get(task.id).result


# ---------------------------------------------------------------- triage


def test_triage_disabled_skips_quietly(tmp_path, monkeypatch, capsys):
    orch = _orch(tmp_path)
    orch.config.triage.model = "none"
    # _make_triage_lm must never be called when triage is disabled.
    monkeypatch.setattr(
        orch, "_make_triage_lm", lambda: (_ for _ in ()).throw(AssertionError("should not build LM"))
    )
    t1 = orch.beads.create(title="a", description="x", assigned_agent="claude")
    t2 = orch.beads.create(title="b", description="x", assigned_agent="claude")
    ordered = orch._triage([t1, t2])
    assert [t.id for t in ordered] == [t1.id, t2.id]  # queue order preserved
    assert "WARNING: triage failed" not in capsys.readouterr().out  # not a failure


def test_triage_down_runs_everything_in_queue_order(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)
    _no_triage_lm(orch, monkeypatch)
    calls: list = []
    _fake_claude_ok(monkeypatch, calls)

    t1 = orch.beads.create(title="a", description="x", assigned_agent="claude")
    t2 = orch.beads.create(title="b", description="x", assigned_agent="claude")

    orch.cycle(now=NOW)
    out = capsys.readouterr().out

    assert "WARNING: triage failed" in out
    assert len(calls) == 2  # nothing deferred or dropped
    assert orch.beads.get(t1.id).status == TaskStatus.DONE
    assert orch.beads.get(t2.id).status == TaskStatus.DONE


def test_triage_defer_holds_task_but_verify_child_always_runs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)
    calls: list = []
    _fake_claude_ok(monkeypatch, calls)

    # A healthy child for the verify bead.
    inst = tmp_path / "kid"
    inst.mkdir()
    (inst / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    (inst / "heartbeat.json").write_text(
        json.dumps({"cycle_completed_at": NOW.isoformat(), "errors_this_cycle": 0})
    )
    orch.children.path.parent.mkdir(parents=True, exist_ok=True)
    orch.children.add(ChildRef(name="kid", path=str(inst), expected_interval="1h"))

    work = orch.beads.create(title="real work", description="x", assigned_agent="claude")
    verify = orch.beads.create(
        title="verify kid",
        description="x",
        metadata={"type": "verify_child", "child": "kid"},
    )

    # Triage says: defer BOTH — the verify hold must be overridden.
    triage_lm = DummyLM(
        [
            {
                "reasoning": "Nothing is urgent.",
                "run_now": [],
                "defer": [work.id, verify.id],
                "needs_human": [],
                "needs_planner": [],
            }
        ],
        reasoning=True,
    )
    monkeypatch.setattr(orch, "_make_triage_lm", lambda: triage_lm)

    orch.cycle(now=NOW)

    assert orch.beads.get(work.id).status == TaskStatus.PENDING  # held
    assert orch.beads.get(verify.id).status == TaskStatus.DONE  # never deferrable
    assert calls == []  # the deferred claude task did not run


# ------------------------------------------------------------ verify_child


def test_verify_child_stale_fails_bead_and_notifies(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = _build_config(tmp_path)
    config.notify.enabled = True
    orch = Orchestrator(config)
    _no_triage_lm(orch, monkeypatch)

    inst = tmp_path / "kid"
    inst.mkdir()
    (inst / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    (inst / "heartbeat.json").write_text(
        json.dumps(
            {"cycle_completed_at": (NOW - timedelta(hours=5)).isoformat()}
        )
    )
    orch.children.path.parent.mkdir(parents=True, exist_ok=True)
    orch.children.add(ChildRef(name="kid", path=str(inst), expected_interval="1h"))

    notified: list = []
    monkeypatch.setattr(
        orchestrator_mod,
        "notify_event",
        lambda cfg, message, urgent=False: notified.append((message, urgent)),
    )

    task = orch.beads.create(
        title="verify kid",
        description="x",
        metadata={"type": "verify_child", "child": "kid"},
    )
    orch.cycle(now=NOW)

    refreshed = orch.beads.get(task.id)
    assert refreshed.status == TaskStatus.FAILED
    assert "stale" in refreshed.result
    assert notified and "kid" in notified[0][0]
    assert notified[0][1] is True  # stale child is an URGENT notification


def test_verify_child_unknown_child_fails_loudly(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)
    _no_triage_lm(orch, monkeypatch)

    task = orch.beads.create(
        title="verify ghost",
        description="x",
        metadata={"type": "verify_child", "child": "ghost"},
    )
    orch.cycle(now=NOW)

    assert orch.beads.get(task.id).status == TaskStatus.FAILED
    assert "unknown child" in capsys.readouterr().out


def test_limit_never_starves_verify_child(tmp_path, monkeypatch):
    """A busy queue with a small cycle limit still runs every verify bead."""
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)
    _no_triage_lm(orch, monkeypatch)
    _fake_claude_ok(monkeypatch)

    inst = tmp_path / "kid"
    inst.mkdir()
    (inst / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    (inst / "heartbeat.json").write_text(
        json.dumps({"cycle_completed_at": NOW.isoformat(), "errors_this_cycle": 0})
    )
    orch.children.path.parent.mkdir(parents=True, exist_ok=True)
    orch.children.add(ChildRef(name="kid", path=str(inst), expected_interval="1h"))

    # Five older work tasks sort ahead of the verify bead in the queue.
    work_ids = [
        orch.beads.create(title=f"w{i}", description="x", assigned_agent="claude").id
        for i in range(5)
    ]
    verify = orch.beads.create(
        title="verify kid",
        description="x",
        metadata={"type": "verify_child", "child": "kid"},
    )

    orch.cycle(now=NOW, limit=2)

    assert orch.beads.get(verify.id).status == TaskStatus.DONE
    done_work = [t for t in work_ids if orch.beads.get(t).status == TaskStatus.DONE]
    assert len(done_work) == 1  # verify took one of the two slots


# --------------------------------------------------------- runs + summary


def test_cycle_appends_structured_run_log(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)
    _no_triage_lm(orch, monkeypatch)
    monkeypatch.setattr(
        orchestrator_mod,
        "run_claude_task",
        lambda prompt, timeout, max_turns, model=None: ExecResult(False, "", "boom exited 1", 1, 0.1),
    )
    orch.beads.create(title="will fail", description="x", assigned_agent="claude")

    orch.cycle(now=NOW)
    # A failure auto-spawns its RCA root bead (agentco/rca.py) — the next
    # cycle picks that up and runs it too (it fails the same way, since
    # run_claude_task is patched to always fail). The RCA bead's own
    # source=="rca" stops it from spawning a further RCA (no RCA-of-RCA),
    # so the THIRD cycle is the genuinely empty one.
    orch.cycle(now=NOW + timedelta(hours=1))
    orch.cycle(now=NOW + timedelta(hours=2))  # empty cycle also logged

    lines = (tmp_path / "runs.jsonl").read_text().strip().splitlines()
    assert len(lines) == 3
    first = json.loads(lines[0])
    assert first["at"] == NOW.isoformat()
    assert first["errors"] == 1
    assert first["tasks"][0]["outcome"] == "failed"
    assert "boom" in first["tasks"][0]["error"]

    second = json.loads(lines[1])
    assert second["errors"] == 1
    assert second["tasks"][0]["title"].startswith("[RCA]")

    assert json.loads(lines[2])["tasks"] == []


def test_crashed_cycle_writes_no_run_record(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)
    monkeypatch.setattr(orch.beads, "ready", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        orch.cycle(now=NOW)
    assert not (tmp_path / "runs.jsonl").exists()


def test_cycle_summary_notification_sent_when_enabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = _build_config(tmp_path)
    config.notify.enabled = True
    config.notify.cycle_summary = True
    orch = Orchestrator(config)
    _no_triage_lm(orch, monkeypatch)
    _fake_claude_ok(monkeypatch)

    sent: list = []
    monkeypatch.setattr(
        orchestrator_mod,
        "notify_event",
        lambda cfg, message, urgent=False: sent.append((message, urgent)),
    )
    orch.beads.create(title="Hourly sync", description="x", assigned_agent="claude")
    orch.cycle(now=NOW)

    assert len(sent) == 1
    message, urgent = sent[0]
    assert urgent is False  # routine summary — telegram only, never voiced
    assert "1 done, 0 errors" in message
    assert "Hourly sync" in message


def test_cycle_summary_off_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)
    _no_triage_lm(orch, monkeypatch)
    sent: list = []
    monkeypatch.setattr(
        orchestrator_mod,
        "notify_event",
        lambda cfg, message, urgent=False: sent.append(message),
    )
    orch.cycle(now=NOW)
    assert sent == []


# ----------------------------------------------------------- claude budget


def test_budget_from_recurring_def_reaches_executor(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)
    _no_triage_lm(orch, monkeypatch)
    calls: list = []
    _fake_claude_ok(monkeypatch, calls)

    orch.recurring.add(
        RecurringDef(
            id="rec-tight",
            title="Tight budget task",
            schedule={"every": "1h"},
            agent="claude",
            payload={"prompt": "be quick"},
            budget={"timeout": 120, "max_turns": 5},
        )
    )
    orch.cycle(now=NOW)

    assert calls == [{"prompt": "be quick", "timeout": 120, "max_turns": 5}]


# ------------------------------------------------------ external agents


def test_external_agent_bead_left_pending(tmp_path, monkeypatch, capsys):
    """A bead assigned to a config-declared agent with no in-process class
    (e.g. sommeliwhey's box-scout, worked by an out-of-band sweep) must be
    left pending — not claimed, failed, and RCA'd (2026-07-22 / 2026-07-29)."""
    monkeypatch.chdir(tmp_path)
    config = _build_config(tmp_path)
    config.agents["box-scout"] = AgentConfig(model="local-model")
    orch = Orchestrator(config)
    _no_triage_lm(orch, monkeypatch)

    task = orch.beads.create(
        title="box-scout: CAFELLOW", description="x", assigned_agent="box-scout"
    )
    orch.cycle(now=NOW)

    assert orch.beads.get(task.id).status == TaskStatus.PENDING
    assert orch.beads.list(assigned_agent="claude") == []  # no RCA spawned
    assert "externally-executed" in capsys.readouterr().out


def test_undeclared_unknown_agent_still_fails_loudly(tmp_path, monkeypatch):
    """A LONE bead naming an undispatchable agent is a typo — it must fail
    (and RCA), never linger silently as pending.

    One bead is the discriminator: a typo belongs to a bead, an un-declared
    agent belongs to the config and therefore shows up on every bead naming it
    (see the ≥2 case below).
    """
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)
    _no_triage_lm(orch, monkeypatch)

    task = orch.beads.create(
        title="typo bead", description="x", assigned_agent="box-scoot"
    )
    orch.cycle(now=NOW)

    refreshed = orch.beads.get(task.id)
    assert refreshed.status == TaskStatus.FAILED
    assert "Unknown agent" in refreshed.result


def test_undeclared_agent_on_many_beads_stalls_instead_of_burning_the_queue(
    tmp_path, monkeypatch, capsys
):
    """The sommeliwhey box-scout incident, on the execution path.

    `agents:` lost its `box-scout` key three times (2026-07-22, 07-29, 08-04).
    Each time the cycle claimed every box-scout bead, failed it with "Unknown
    agent: box-scout", and spawned an RCA bead apiece — 50 beads and ~95
    duplicate RCAs at ~$6 each for one missing config key. A name that accounts
    for two or more ready beads is config-shaped, so the cycle must leave those
    beads pending and say so once, loudly.
    """
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)  # config.agents has no box-scout — the clobbered state
    _no_triage_lm(orch, monkeypatch)

    tasks = [
        orch.beads.create(
            title=f"box-scout: brand {i}", description="x", assigned_agent="box-scout"
        )
        for i in range(3)
    ]
    summary = orch.cycle(now=NOW)

    for task in tasks:
        refreshed = orch.beads.get(task.id)
        assert refreshed.status == TaskStatus.PENDING
        assert refreshed.result is None
    assert orch.beads.list(assigned_agent="claude") == []  # no RCA fan-out
    assert summary["errors"] == 0
    assert summary["undispatchable"] == 3

    out = capsys.readouterr().out
    assert "UNDISPATCHABLE_AGENT" in out
    assert "box-scout" in out


def test_stalled_beads_run_once_the_config_declares_them(tmp_path, monkeypatch):
    """The stall is not a dead end: restoring the config is the whole fix.

    This is the recovery the three incidents needed — put the key back and the
    next cycle proceeds. Here the declaration makes the beads externally-executed
    (declared, no in-process class), so they stay pending for their out-of-band
    worker rather than being claimed — and, crucially, they are no longer
    reported as undispatchable.
    """
    monkeypatch.chdir(tmp_path)
    config = _build_config(tmp_path)
    orch = Orchestrator(config)
    _no_triage_lm(orch, monkeypatch)
    for i in range(3):
        orch.beads.create(
            title=f"box-scout: brand {i}", description="x", assigned_agent="box-scout"
        )

    assert orch.cycle(now=NOW)["undispatchable"] == 3

    config.agents["box-scout"] = AgentConfig(model="local-model")  # the repair
    summary = Orchestrator(config).cycle(now=NOW)

    assert "undispatchable" not in summary
    assert summary["errors"] == 0


def test_undispatchable_count_reaches_the_run_log(tmp_path, monkeypatch):
    """runs.jsonl is where a later RCA reconstructs the day — the signal that
    explains an idle cycle has to survive in it, not just in stdout."""
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)
    _no_triage_lm(orch, monkeypatch)
    for i in range(2):
        orch.beads.create(
            title=f"box-scout: brand {i}", description="x", assigned_agent="box-scout"
        )

    orch.cycle(now=NOW)

    record = json.loads((tmp_path / "runs.jsonl").read_text().strip().split("\n")[-1])
    assert record["undispatchable"] == 2


def test_unassigned_bead_is_blocked_not_looped(tmp_path):
    """An unassigned bead must fail ONCE, terminally — never every hour, mutely.

    Regression for 2026-08-04 (sommeliwhey). _execute_task returned False for an
    unassigned bead without touching its state, so cycle() counted an error, the
    bead stayed PENDING with result=None, and ready() re-selected it next hour.
    24 beads did that from 2026-07-24 onward, pinning `errors` non-zero in
    runs.jsonl and heartbeat.json for 11 days and making the box-scout incidents
    invisible in the one signal that should have caught them.
    """
    from agentco_harness.beads import Beads, TaskStatus
    from agentco_harness.config import Config, LLMConfig
    from agentco_harness.orchestrator import Orchestrator

    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    config.llm = LLMConfig(default_provider="lmstudio", default_model="local-model")
    config.notify.enabled = False
    orch = Orchestrator(config)

    task = orch.beads.create(title="BD-D3: secao galeria", description="d")
    assert orch._execute_task(task) is False

    refreshed = orch.beads.get(task.id)
    assert refreshed.status == TaskStatus.BLOCKED
    assert refreshed.result and "no assigned_agent" in refreshed.result

    # Terminal means terminal: it is not handed back to the next cycle.
    assert task.id not in {t.id for t in Beads(config.tasks_path).ready()}


# ------------------------------------------------------------ chat_pending
# (the comment-loop fix — a human comment on a bead must reach an agent even
# though ready()/`_execute_cycle_task` refuse to dispatch a human-assigned
# bead at all)


def test_chat_pending_gets_an_agent_reply_and_flag_clears(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)
    _no_triage_lm(orch, monkeypatch)
    calls: list = []

    def fake(prompt, timeout, max_turns, model=None):
        calls.append(prompt)
        return ExecResult(True, "almost nothing is pending on you", None, 0, 0.1)

    monkeypatch.setattr(orchestrator_mod, "run_claude_task", fake)

    task = orch.beads.create(
        title="Nightly release radar",
        description="d",
        assigned_to="human:mabidoli",
        metadata={
            "chat": [{"type": "human", "text": "what's pending?", "at": NOW.isoformat()}],
            "chat_pending": True,
            "chat_pending_at": NOW.isoformat(),
        },
    )

    orch.cycle(now=NOW)

    assert len(calls) == 1
    assert task.title in calls[0]
    refreshed = orch.beads.get(task.id)
    chat = refreshed.metadata["chat"]
    assert chat[-1]["type"] == "agent"
    assert chat[-1]["text"] == "almost nothing is pending on you"
    assert "chat_pending" not in refreshed.metadata
    assert "chat_pending_at" not in refreshed.metadata


def test_chat_reply_never_touches_status_or_assignment(tmp_path, monkeypatch):
    """The core safety property: answering a comment must be indistinguishable
    from a no-op except for the chat thread itself."""
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)
    _no_triage_lm(orch, monkeypatch)
    _fake_claude_ok(monkeypatch)

    task = orch.beads.create(
        title="human task",
        description="d",
        assigned_to="human:mabidoli",
        status=TaskStatus.PENDING,
        metadata={
            "chat": [{"type": "human", "text": "hi", "at": NOW.isoformat()}],
            "chat_pending": True,
        },
    )

    orch.cycle(now=NOW)

    refreshed = orch.beads.get(task.id)
    assert refreshed.status == TaskStatus.PENDING
    assert refreshed.assigned_to == "human:mabidoli"
    assert refreshed.assigned_agent is None


def test_chat_pending_does_not_dispatch_through_ready(tmp_path, monkeypatch):
    """A human-assigned chat_pending bead never enters the normal ready()
    execution loop — only the dedicated chat-reply step ever touches it."""
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)
    _no_triage_lm(orch, monkeypatch)
    _fake_claude_ok(monkeypatch)

    task = orch.beads.create(
        title="human task",
        description="d",
        assigned_to="human:mabidoli",
        metadata={"chat": [], "chat_pending": True},
    )

    assert task.id not in {t.id for t in orch.beads.ready()}
    orch.cycle(now=NOW)
    # still not ready-dispatched — the bead is untouched by _execute_cycle_task
    assert orch.beads.get(task.id).status == TaskStatus.PENDING


def test_no_pending_chats_is_a_quiet_noop(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)
    _no_triage_lm(orch, monkeypatch)
    summary = orch.cycle(now=NOW)
    assert "chat_replies" not in summary


def test_chat_reply_failure_is_recorded_and_flag_still_clears(tmp_path, monkeypatch):
    """A failed subagent answer must not leave the bead stuck retrying forever
    — the failure itself becomes the (honest) reply."""
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)
    _no_triage_lm(orch, monkeypatch)
    monkeypatch.setattr(
        orchestrator_mod,
        "run_claude_task",
        lambda prompt, timeout, max_turns, model=None: ExecResult(False, "", "boom", 1, 0.1),
    )

    task = orch.beads.create(
        title="human task",
        description="d",
        assigned_to="human:mabidoli",
        metadata={"chat": [], "chat_pending": True},
    )

    orch.cycle(now=NOW)

    refreshed = orch.beads.get(task.id)
    assert "chat_pending" not in refreshed.metadata
    assert "boom" in refreshed.metadata["chat"][-1]["text"]
    assert refreshed.metadata["chat"][-1]["type"] == "agent"


def test_concurrent_chat_dispatch_does_not_double_answer(tmp_path, monkeypatch):
    """The immediate-dispatch path (webui.api_chat's background task) and the
    cycle's safety-net sweep racing for the SAME bead must not both call the
    agent — the CAS lease (`_claim_chat_lease`) admits exactly one."""
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)
    _no_triage_lm(orch, monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        orchestrator_mod,
        "run_claude_task",
        lambda prompt, timeout, max_turns, model=None: (
            calls.append(1) or ExecResult(True, "answered", None, 0, 0.1)
        ),
    )

    task = orch.beads.create(
        title="race",
        description="d",
        assigned_to="human:mabidoli",
        metadata={"chat": [], "chat_pending": True},
    )

    # Simulate: the webui POST path already claimed the lease and is still
    # "in flight" (e.g. a slow subagent) when the cycle sweep runs.
    from agentco_harness.orchestrator import _claim_chat_lease

    claimed = _claim_chat_lease(orch.beads, task.id)
    assert claimed is not None

    orch.cycle(now=NOW)  # the sweep must see the live lease and back off

    assert calls == []
    refreshed = orch.beads.get(task.id)
    # Still unanswered — the in-flight caller (not this cycle) owns finishing it.
    assert refreshed.metadata.get("chat_pending") is True


def test_second_claim_of_a_live_lease_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)
    from agentco_harness.orchestrator import _claim_chat_lease

    task = orch.beads.create(
        title="race",
        description="d",
        assigned_to="human:mabidoli",
        metadata={"chat": [], "chat_pending": True},
    )
    first = _claim_chat_lease(orch.beads, task.id, now=NOW)
    assert first is not None
    second = _claim_chat_lease(orch.beads, task.id, now=NOW)
    assert second is None


def test_stale_chat_lease_is_reclaimed(tmp_path, monkeypatch):
    """A holder that crashed mid-run must not block the bead forever — a
    lease older than the TTL is treated as abandoned."""
    monkeypatch.chdir(tmp_path)
    orch = _orch(tmp_path)
    _no_triage_lm(orch, monkeypatch)
    from agentco_harness.orchestrator import _CHAT_LEASE_TTL_S

    stale_at = (NOW - timedelta(seconds=_CHAT_LEASE_TTL_S + 60)).isoformat()
    task = orch.beads.create(
        title="stale lease",
        description="d",
        assigned_to="human:mabidoli",
        metadata={"chat": [], "chat_pending": True, "chat_in_flight_at": stale_at},
    )
    monkeypatch.setattr(
        orchestrator_mod,
        "run_claude_task",
        lambda prompt, timeout, max_turns, model=None: ExecResult(True, "ok", None, 0, 0.1),
    )

    orch.cycle(now=NOW)

    refreshed = orch.beads.get(task.id)
    assert "chat_pending" not in refreshed.metadata
    assert refreshed.metadata["chat"][-1]["text"] == "ok"
