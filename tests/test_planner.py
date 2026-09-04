"""Delegation Layer — Stage 2: planner bead + tier registry.

Covers:
  - tiers config parse / defaults / unknown-tier (deferred `local`) warning
  - planner task routes through the store-backed claude path on tiers['planner']
  - decompose → subtasks pending_approval with executor_tier / acceptance /
    proposed_assigned_to metadata; MAX_SUBTASKS_PER_TASK + depth caps enforced
  - route → proposal recorded on the parent, nothing auto-applied
  - execute_directly → recommendation recorded, planner does no work
  - executor_tier resolves to a model at claude dispatch; unknown tier degrades loud
  - triage extension (needs_planner surfaced, never spawns; absent degrades safely)

All offline: fake subprocess (monkeypatched run_store_backed_task/run_claude_task)
and a fake LM (DummyLM). No network, no API keys.
"""

from __future__ import annotations

import json

import yaml
from dspy.utils.dummies import DummyLM

import agentco_harness.orchestrator as orchestrator_mod
from agentco_harness.beads import MAX_SUBTASK_DEPTH, MAX_SUBTASKS_PER_TASK, TaskResult, TaskStatus
from agentco_harness.config import AgentConfig, Config, LLMConfig, TiersConfig
from agentco_harness.executor import ExecResult
from agentco_harness.orchestrator import Orchestrator
from agentco_harness.triage import _summarize, triage_order


# --------------------------------------------------------------- fixtures


def _orch(tmp_path) -> Orchestrator:
    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    config.llm = LLMConfig(default_provider="lmstudio", default_model="local-model")
    config.agents = {"pm": AgentConfig(model="local-model")}
    config.notify.enabled = False
    return Orchestrator(config)


def _decision_result(decision: dict) -> str:
    """Wrap a decision dict in a TaskResult whose `output` is the decision JSON string."""
    return TaskResult(status="complete", output=json.dumps(decision)).to_json()


def _fake_planner_run(orch, decision: dict, captured: dict):
    """Return a fake run_store_backed_task that records model/prompt and simulates
    the subagent writing the decision back to the store."""

    def fake(task_id, config_path, timeout, max_turns, model=None, prompt=None):
        captured["model"] = model
        captured["prompt"] = prompt
        orch.beads.complete(task_id, result=_decision_result(decision))
        return ExecResult(True, "ok", None, 0, 0.1)

    return fake


# ------------------------------------------------------------- tiers config


def test_tiers_defaults_when_absent():
    config = Config()
    assert config.tiers.model_for("planner") == "claude-fable-5"
    assert config.tiers.model_for("worker") == "claude-sonnet-5"
    assert config.tiers.model_for("executor") == "claude-haiku-4-5"


def test_tiers_no_local_tier_by_default():
    # `local` is deferred (ISA Out of Scope) — it must NOT exist.
    config = Config()
    assert config.tiers.model_for("local") is None
    assert "local" not in config.tiers.models


def test_tiers_parsed_and_overridable(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {"tasks_path": "tasks.jsonl", "tiers": {"planner": "claude-opus-9", "worker": "claude-sonnet-5"}}
        )
    )
    config = Config.load(cfg_file)
    assert config.tiers.model_for("planner") == "claude-opus-9"  # overridden
    assert config.tiers.model_for("worker") == "claude-sonnet-5"
    assert config.tiers.model_for("executor") == "claude-haiku-4-5"  # default kept


def test_tiers_unknown_key_warns_and_is_dropped(tmp_path, capsys):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {"tasks_path": "tasks.jsonl", "tiers": {"planner": "claude-fable-5", "local": "lmstudio/qwen"}}
        )
    )
    config = Config.load(cfg_file)
    out = capsys.readouterr().out
    assert "nothing consumes" in out
    assert "local" in out
    assert "tiers" in out
    # Dropped, not silently kept.
    assert config.tiers.model_for("local") is None
    assert "local" not in config.tiers.models


def test_tiers_round_trip_through_save(tmp_path):
    config = Config()
    config.tiers = TiersConfig(models={"planner": "p", "worker": "w", "executor": "e"})
    cfg_file = tmp_path / "config.yaml"
    config.save(cfg_file)

    dumped = yaml.safe_load(cfg_file.read_text())
    assert dumped["tiers"] == {"planner": "p", "worker": "w", "executor": "e"}
    reloaded = Config.load(cfg_file)
    assert reloaded.tiers.model_for("planner") == "p"


# ---------------------------------------------- planner store-backed routing


def test_planner_routes_through_store_backed_with_planner_tier_model(tmp_path, monkeypatch):
    orch = _orch(tmp_path)
    captured: dict = {}
    monkeypatch.setattr(
        orchestrator_mod,
        "run_store_backed_task",
        _fake_planner_run(orch, {"decision": "execute_directly", "rationale": "atomic"}, captured),
    )
    task = orch.beads.create(title="plan me", description="x", assigned_agent="planner")

    assert orch._execute_cycle_task(task) is True
    # The planner bead ran on the planner tier's model...
    assert captured["model"] == "claude-fable-5"
    # ...via a planner-specific prompt (store-backed decision contract).
    assert "PLANNER" in captured["prompt"]
    assert "tasks complete" in captured["prompt"]


def test_planner_explicit_model_override_beats_tier(tmp_path, monkeypatch):
    orch = _orch(tmp_path)
    captured: dict = {}
    monkeypatch.setattr(
        orchestrator_mod,
        "run_store_backed_task",
        _fake_planner_run(orch, {"decision": "execute_directly"}, captured),
    )
    task = orch.beads.create(
        title="plan me", description="x", assigned_agent="planner", metadata={"model": "claude-opus-9"}
    )
    assert orch._execute_cycle_task(task) is True
    assert captured["model"] == "claude-opus-9"


def test_planner_missing_result_fails_loudly(tmp_path, monkeypatch, capsys):
    orch = _orch(tmp_path)

    # Subprocess "succeeds" but the planner never wrote the decision back.
    monkeypatch.setattr(
        orchestrator_mod,
        "run_store_backed_task",
        lambda task_id, config_path, timeout, max_turns, model=None, prompt=None: ExecResult(
            True, "", None, 0, 0.1
        ),
    )
    task = orch.beads.create(title="plan me", description="x", assigned_agent="planner")
    assert orch._execute_cycle_task(task) is False
    refreshed = orch.beads.get(task.id)
    assert refreshed.status == TaskStatus.FAILED
    assert "result missing from store" in (refreshed.result or "")


def test_planner_subprocess_failure_fails_loudly(tmp_path, monkeypatch):
    orch = _orch(tmp_path)
    monkeypatch.setattr(
        orchestrator_mod,
        "run_store_backed_task",
        lambda task_id, config_path, timeout, max_turns, model=None, prompt=None: ExecResult(
            False, "", "planner subagent timed out", None, 0.1
        ),
    )
    task = orch.beads.create(title="plan me", description="x", assigned_agent="planner")
    assert orch._execute_cycle_task(task) is False
    assert orch.beads.get(task.id).status == TaskStatus.FAILED


def test_planner_unparseable_decision_fails_loudly(tmp_path, monkeypatch):
    orch = _orch(tmp_path)

    def fake(task_id, config_path, timeout, max_turns, model=None, prompt=None):
        # Valid TaskResult, but `output` is not decision JSON.
        orch.beads.complete(task_id, result=TaskResult(status="complete", output="not json").to_json())
        return ExecResult(True, "ok", None, 0, 0.1)

    monkeypatch.setattr(orchestrator_mod, "run_store_backed_task", fake)
    task = orch.beads.create(title="plan me", description="x", assigned_agent="planner")
    assert orch._execute_cycle_task(task) is False
    assert orch.beads.get(task.id).status == TaskStatus.FAILED


def test_planner_unknown_decision_kind_fails_loudly(tmp_path, monkeypatch):
    orch = _orch(tmp_path)
    captured: dict = {}
    monkeypatch.setattr(
        orchestrator_mod,
        "run_store_backed_task",
        _fake_planner_run(orch, {"decision": "teleport"}, captured),
    )
    task = orch.beads.create(title="plan me", description="x", assigned_agent="planner")
    assert orch._execute_cycle_task(task) is False
    assert orch.beads.get(task.id).status == TaskStatus.FAILED


# ------------------------------------------------------------- decompose


def test_planner_decompose_creates_pending_approval_subtasks_with_metadata(tmp_path, monkeypatch):
    orch = _orch(tmp_path)
    decision = {
        "decision": "decompose",
        "subtasks": [
            {
                "title": "write the parser",
                "description": "parse the file",
                "proposed_assigned_to": "human:mabidoli",
                "executor_tier": "worker",
                "acceptance_criteria": ["parses valid input", "rejects garbage"],
            }
        ],
    }
    captured: dict = {}
    monkeypatch.setattr(
        orchestrator_mod, "run_store_backed_task", _fake_planner_run(orch, decision, captured)
    )
    parent = orch.beads.create(title="big feature", description="x", assigned_agent="planner")

    assert orch._execute_cycle_task(parent) is True

    children = [t for t in orch.beads.list() if t.parent_id == parent.id]
    assert len(children) == 1
    child = children[0]
    assert child.status == TaskStatus.PENDING_APPROVAL
    assert child.metadata["executor_tier"] == "worker"
    assert child.metadata["acceptance_criteria"] == ["parses valid input", "rejects garbage"]
    assert child.metadata["proposed_assigned_to"] == "human:mabidoli"
    assert child.metadata["requires_approval"] is True
    # Human-lineage safety: the proposal is metadata only — assigned_agent is NOT
    # set from a human token by the planner.
    assert child.assigned_agent is None


def test_planner_decompose_enforces_max_subtasks_cap(tmp_path, monkeypatch, capsys):
    orch = _orch(tmp_path)
    decision = {
        "decision": "decompose",
        "subtasks": [
            {"title": f"st{i}", "description": "d"}
            for i in range(MAX_SUBTASKS_PER_TASK + 1)  # one over the cap
        ],
    }
    captured: dict = {}
    monkeypatch.setattr(
        orchestrator_mod, "run_store_backed_task", _fake_planner_run(orch, decision, captured)
    )
    parent = orch.beads.create(title="huge", description="x", assigned_agent="planner")

    assert orch._execute_cycle_task(parent) is True
    children = [t for t in orch.beads.list() if t.parent_id == parent.id]
    assert len(children) == MAX_SUBTASKS_PER_TASK  # capped
    assert f"capping at {MAX_SUBTASKS_PER_TASK}" in capsys.readouterr().out


def test_planner_decompose_enforces_depth_cap(tmp_path, monkeypatch, capsys):
    orch = _orch(tmp_path)
    # Build a chain so the planner task sits at MAX_SUBTASK_DEPTH (2). Its children
    # would be depth 3 → refused by the depth cap in beads.create.
    node = orch.beads.create(title="root", description="d0")
    for gen in range(1, MAX_SUBTASK_DEPTH):
        node = orch.beads.create(title=f"d{gen}", description=f"d{gen}", parent_id=node.id)
    planner_task = orch.beads.create(
        title="planner at cap", description="d", assigned_agent="planner", parent_id=node.id
    )
    assert orch.beads._depth_of(planner_task.id) == MAX_SUBTASK_DEPTH

    decision = {"decision": "decompose", "subtasks": [{"title": "too deep", "description": "d"}]}
    captured: dict = {}
    monkeypatch.setattr(
        orchestrator_mod, "run_store_backed_task", _fake_planner_run(orch, decision, captured)
    )

    # Every child was depth-capped → 0 created → decompose FAILS loudly (MEDIUM-6:
    # a decompose that yields no real subtasks must not report success).
    assert orch._execute_cycle_task(planner_task) is False
    children = [t for t in orch.beads.list() if t.parent_id == planner_task.id]
    assert children == []  # depth cap refused every child
    out = capsys.readouterr().out
    assert "depth cap" in out
    assert orch.beads.get(planner_task.id).status == TaskStatus.FAILED


def test_planner_decompose_with_no_subtasks_fails_loudly(tmp_path, monkeypatch):
    orch = _orch(tmp_path)
    captured: dict = {}
    monkeypatch.setattr(
        orchestrator_mod,
        "run_store_backed_task",
        _fake_planner_run(orch, {"decision": "decompose", "subtasks": []}, captured),
    )
    task = orch.beads.create(title="empty", description="x", assigned_agent="planner")
    assert orch._execute_cycle_task(task) is False
    assert orch.beads.get(task.id).status == TaskStatus.FAILED


# ---------------------------------------------------------------- route


def test_planner_route_records_proposal_and_applies_nothing(tmp_path, monkeypatch):
    orch = _orch(tmp_path)
    decision = {
        "decision": "route",
        "proposed_assigned_to": "human:mabidoli",
        "proposed_agent": "dev",
        "rationale": "needs a human eye",
    }
    captured: dict = {}
    monkeypatch.setattr(
        orchestrator_mod, "run_store_backed_task", _fake_planner_run(orch, decision, captured)
    )
    task = orch.beads.create(title="route me", description="x", assigned_agent="planner")

    assert orch._execute_cycle_task(task) is True
    refreshed = orch.beads.get(task.id)
    # The proposal is recorded...
    assert refreshed.metadata["proposed_route"]["proposed_assigned_to"] == "human:mabidoli"
    assert refreshed.metadata["proposed_route"]["proposed_agent"] == "dev"
    assert refreshed.metadata["proposed_route"]["rationale"] == "needs a human eye"
    # ...but NOTHING was auto-applied: the assignment is untouched and no subtasks exist.
    assert refreshed.assigned_agent == "planner"
    assert [t for t in orch.beads.list() if t.parent_id == task.id] == []


def test_planner_route_never_flips_human_assignment_to_agent(tmp_path, monkeypatch):
    """The human-lineage invariant: a route proposal never mutates assigned_agent,
    so nothing human-lineaged can be converted to an agent assignment."""
    orch = _orch(tmp_path)
    decision = {"decision": "route", "proposed_agent": "dev", "rationale": "faster"}
    captured: dict = {}
    monkeypatch.setattr(
        orchestrator_mod, "run_store_backed_task", _fake_planner_run(orch, decision, captured)
    )
    # Simulate a human-lineaged planner task (assigned_to is a Stage 1 concept; we
    # stash it in metadata to prove Stage 2 never touches the live assignment).
    task = orch.beads.create(
        title="human work", description="x", assigned_agent="planner",
        metadata={"proposed_assigned_to": "human:mabidoli"},
    )
    assert orch._execute_cycle_task(task) is True
    refreshed = orch.beads.get(task.id)
    # The route is a proposal; assigned_agent was never set to "dev".
    assert refreshed.assigned_agent == "planner"
    assert "proposed_route" in refreshed.metadata


# ---------------------------------------------------------- execute_directly


def test_planner_execute_directly_records_recommendation_only(tmp_path, monkeypatch):
    orch = _orch(tmp_path)
    captured: dict = {}
    monkeypatch.setattr(
        orchestrator_mod,
        "run_store_backed_task",
        _fake_planner_run(orch, {"decision": "execute_directly", "rationale": "single unit"}, captured),
    )
    task = orch.beads.create(title="atomic", description="x", assigned_agent="planner")

    assert orch._execute_cycle_task(task) is True
    refreshed = orch.beads.get(task.id)
    assert refreshed.metadata["planner_recommendation"] == "execute_directly"
    assert refreshed.metadata["planner_rationale"] == "single unit"
    # The planner did no work itself and spawned nothing.
    assert [t for t in orch.beads.list() if t.parent_id == task.id] == []


# ---------------------------------------------------- executor_tier at dispatch


def _fake_claude_capture(monkeypatch, captured: dict):
    def fake(prompt, timeout, max_turns, model=None, cwd=None):
        captured["model"] = model
        return ExecResult(True, '{"ok": true}', None, 0, 0.1)

    monkeypatch.setattr(orchestrator_mod, "run_claude_task", fake)


def test_executor_tier_resolves_to_model_at_dispatch(tmp_path, monkeypatch):
    orch = _orch(tmp_path)
    captured: dict = {}
    _fake_claude_capture(monkeypatch, captured)
    task = orch.beads.create(
        title="atomic", description="x", assigned_agent="claude", metadata={"executor_tier": "worker"}
    )
    assert orch._execute_claude_task(task) is True
    assert captured["model"] == "claude-sonnet-5"


def test_explicit_model_metadata_beats_executor_tier(tmp_path, monkeypatch):
    orch = _orch(tmp_path)
    captured: dict = {}
    _fake_claude_capture(monkeypatch, captured)
    task = orch.beads.create(
        title="atomic", description="x", assigned_agent="claude",
        metadata={"executor_tier": "worker", "model": "claude-opus-9"},
    )
    assert orch._execute_claude_task(task) is True
    assert captured["model"] == "claude-opus-9"


def test_unknown_executor_tier_degrades_loudly_to_default(tmp_path, monkeypatch, capsys):
    orch = _orch(tmp_path)
    captured: dict = {}
    _fake_claude_capture(monkeypatch, captured)
    task = orch.beads.create(
        title="atomic", description="x", assigned_agent="claude", metadata={"executor_tier": "bogus"}
    )
    assert orch._execute_claude_task(task) is True
    assert captured["model"] is None  # fell back to the CLI default
    out = capsys.readouterr().out
    assert "unknown executor_tier" in out
    assert "bogus" in out


def test_no_tier_no_model_inherits_cli_default(tmp_path, monkeypatch):
    orch = _orch(tmp_path)
    captured: dict = {}
    _fake_claude_capture(monkeypatch, captured)
    task = orch.beads.create(title="plain", description="x", assigned_agent="claude")
    assert orch._execute_claude_task(task) is True
    assert captured["model"] is None


# ---------------------------------------------------- triage extension (advisory)


def test_triage_summary_includes_proposed_and_assigned_info(tmp_path):
    orch = _orch(tmp_path)
    t = orch.beads.create(
        title="a", description="x", assigned_agent="planner",
        metadata={"proposed_assigned_to": "human:mabidoli"},
    )
    summary = json.loads(_summarize([orch.beads.get(t.id)]))
    assert summary[0]["proposed_assigned_to"] == "human:mabidoli"
    assert "assigned_to" in summary[0]  # present (None until Stage 1 lands)


def test_triage_surfaces_needs_planner_without_spawning(tmp_path, capsys):
    orch = _orch(tmp_path)
    t1 = orch.beads.create(title="a", description="x", assigned_agent="claude")
    t2 = orch.beads.create(title="b", description="x", assigned_agent="claude")
    tasks = [orch.beads.get(t1.id), orch.beads.get(t2.id)]

    lm = DummyLM(
        [
            {
                "reasoning": "t1 is big",
                "run_now": [t1.id, t2.id],
                "defer": [],
                "needs_human": [],
                "needs_planner": [t1.id],
            }
        ],
        reasoning=True,
    )
    ordered = triage_order(tasks, lm)
    # needs_planner does NOT change execution order — it is advisory only.
    assert [t.id for t in ordered] == [t1.id, t2.id]
    out = capsys.readouterr().out
    assert "needs_planner" in out
    assert "NOT auto-spawning" in out
    # Structurally guaranteed: triage_order has no Beads handle, so it cannot
    # create a planner bead. No new tasks exist.
    assert len(orch.beads.list()) == 2


def test_triage_absent_needs_planner_degrades_to_queue_order(tmp_path, monkeypatch, capsys):
    """A triage LM that omits needs_planner makes the adapter raise — the caller
    degrades to queue order with a WARNING, exactly as any triage failure does."""
    orch = _orch(tmp_path)
    t1 = orch.beads.create(title="a", description="x", assigned_agent="claude")
    t2 = orch.beads.create(title="b", description="x", assigned_agent="claude")
    tasks = [orch.beads.get(t1.id), orch.beads.get(t2.id)]

    # DummyLM answer WITHOUT needs_planner — the grown contract is unmet.
    lm = DummyLM(
        [{"reasoning": "r", "run_now": [t2.id], "defer": [t1.id], "needs_human": []}], reasoning=True
    )
    monkeypatch.setattr(orch, "_make_triage_lm", lambda: lm)

    ordered = orch._triage(tasks)
    # Fallback ran everything in queue order (the defer was NOT honored).
    assert [t.id for t in ordered] == [t1.id, t2.id]
    assert "WARNING: triage failed" in capsys.readouterr().out


# ---------------------------------- HIGH-4: planner decisions surface in `me` + notify


def test_planner_execute_directly_surfaces_via_needs_input(tmp_path, monkeypatch):
    """execute_directly must leave the DONE planner bead carrying a needs_input
    TaskResult — that is exactly what `agentco me` lists (DONE + needs_input);
    otherwise the recommendation lands on a DONE+complete bead nobody sees."""
    orch = _orch(tmp_path)
    captured: dict = {}
    monkeypatch.setattr(
        orchestrator_mod, "run_store_backed_task",
        _fake_planner_run(orch, {"decision": "execute_directly", "rationale": "atomic unit"}, captured),
    )
    task = orch.beads.create(title="atomic", description="x", assigned_agent="planner")

    assert orch._execute_cycle_task(task) is True
    refreshed = orch.beads.get(task.id)
    assert refreshed.status == TaskStatus.DONE
    tr = TaskResult.from_task(refreshed)
    assert tr is not None and tr.status == "needs_input"
    assert "atomic unit" in (tr.continuation_hint or tr.output or "")
    # Recommendation metadata is still recorded.
    assert refreshed.metadata["planner_recommendation"] == "execute_directly"


def test_planner_route_surfaces_via_needs_input(tmp_path, monkeypatch):
    orch = _orch(tmp_path)
    decision = {"decision": "route", "proposed_agent": "dev", "rationale": "faster over there"}
    captured: dict = {}
    monkeypatch.setattr(
        orchestrator_mod, "run_store_backed_task", _fake_planner_run(orch, decision, captured)
    )
    task = orch.beads.create(title="route me", description="x", assigned_agent="planner")

    assert orch._execute_cycle_task(task) is True
    refreshed = orch.beads.get(task.id)
    tr = TaskResult.from_task(refreshed)
    assert tr is not None and tr.status == "needs_input"
    assert "re-routing" in (tr.continuation_hint or "")
    # The proposal is still recorded on the bead.
    assert refreshed.metadata["proposed_route"]["proposed_agent"] == "dev"


def test_planner_notifies_on_every_decision_branch(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        orchestrator_mod,
        "notify_event",
        lambda cfg, msg, urgent=False: calls.append(msg) or ["telegram"],
    )
    decisions = (
        {"decision": "execute_directly", "rationale": "r"},
        {"decision": "decompose", "subtasks": [{"title": "st", "description": "d"}]},
        {"decision": "route", "proposed_agent": "dev", "rationale": "r"},
    )
    for decision in decisions:
        orch = _orch(tmp_path)
        orch.config.notify.enabled = True
        captured: dict = {}
        monkeypatch.setattr(
            orchestrator_mod, "run_store_backed_task", _fake_planner_run(orch, decision, captured)
        )
        task = orch.beads.create(title="t", description="x", assigned_agent="planner")
        assert orch._execute_cycle_task(task) is True

    assert len(calls) == 3  # every decision branch notified (best-effort)


# ---------------------------------- MEDIUM-6: decompose that creates nothing fails


def test_planner_decompose_all_subtasks_skipped_fails(tmp_path, monkeypatch):
    orch = _orch(tmp_path)
    # Every proposed subtask is a non-object → all skipped → 0 created → FAIL,
    # never a false success.
    decision = {"decision": "decompose", "subtasks": ["nope", 42, None]}
    captured: dict = {}
    monkeypatch.setattr(
        orchestrator_mod, "run_store_backed_task", _fake_planner_run(orch, decision, captured)
    )
    task = orch.beads.create(title="junk", description="x", assigned_agent="planner")
    assert orch._execute_cycle_task(task) is False
    assert orch.beads.get(task.id).status == TaskStatus.FAILED


# ---------------------------------- LOW-10: unknown executor_tier skipped at creation


def test_planner_decompose_skips_unknown_tier_subtask(tmp_path, monkeypatch, capsys):
    orch = _orch(tmp_path)
    decision = {
        "decision": "decompose",
        "subtasks": [
            {"title": "good", "description": "d", "executor_tier": "worker"},
            {"title": "bad", "description": "d", "executor_tier": "bogus"},
        ],
    }
    captured: dict = {}
    monkeypatch.setattr(
        orchestrator_mod, "run_store_backed_task", _fake_planner_run(orch, decision, captured)
    )
    task = orch.beads.create(title="mix", description="x", assigned_agent="planner")

    assert orch._execute_cycle_task(task) is True
    titles = {t.title for t in orch.beads.list() if t.parent_id == task.id}
    assert titles == {"good"}  # unknown-tier subtask refused at creation
    out = capsys.readouterr().out
    assert "unknown executor_tier" in out and "bogus" in out


# ---------------------------------- MEDIUM-7: dispatch defense-in-depth


def test_dispatch_quarantines_pending_task_with_requires_approval(tmp_path):
    orch = _orch(tmp_path)
    # Anomaly: a PENDING task still flagged requires_approval reached dispatch
    # without passing the approval gate — quarantine BLOCKED, never execute.
    t = orch.beads.create(
        "leaked", "x", assigned_agent="claude", metadata={"requires_approval": True}
    )
    assert orch._execute_cycle_task(t) is False
    assert orch.beads.get(t.id).status == TaskStatus.BLOCKED


def test_approved_subtask_dispatches_after_approval(tmp_path, monkeypatch):
    orch = _orch(tmp_path)
    captured: dict = {}
    _fake_claude_capture(monkeypatch, captured)
    sub = orch.beads.create(
        "proposal", "x", assigned_agent="claude",
        status=TaskStatus.PENDING_APPROVAL, metadata={"requires_approval": True},
    )
    approved = orch.beads.approve(sub.id)
    assert approved.status == TaskStatus.PENDING
    # approve() cleared requires_approval, so the guard lets it run.
    assert orch._execute_cycle_task(orch.beads.get(sub.id)) is True
    assert orch.beads.get(sub.id).status == TaskStatus.DONE


# ---------------------------------- LOW-9: malformed needs_planner warns, not silent


def test_triage_malformed_needs_planner_warns(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    import agentco_harness.triage as triage_mod

    orch = _orch(tmp_path)
    t1 = orch.beads.create(title="a", description="x", assigned_agent="claude")
    tasks = [orch.beads.get(t1.id)]

    class FakeCoT:
        def __init__(self, sig):
            pass

        def __call__(self, open_tasks):
            # needs_planner present but the WRONG shape (a bare string).
            return SimpleNamespace(
                run_now=[t1.id], defer=[], needs_human=[], needs_planner="oops-not-a-list"
            )

    monkeypatch.setattr(triage_mod.dspy, "ChainOfThought", FakeCoT)
    ordered = triage_order(tasks, lm=None)
    assert [t.id for t in ordered] == [t1.id]  # order unaffected
    assert "needs_planner is not a list" in capsys.readouterr().out
