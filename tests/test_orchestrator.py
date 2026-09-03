"""Orchestrator wiring: provider inference, per-agent overrides, context flow."""

from __future__ import annotations

import dspy
from dspy.utils.dummies import DummyLM

from agentco_harness.agents import DevAgent, DevOpsAgent, PMAgent
from agentco_harness.beads import Beads, TaskStatus, MAX_SUBTASKS_PER_TASK
from agentco_harness.config import AgentConfig, Config, LLMConfig
from agentco_harness.orchestrator import Orchestrator, infer_provider

from .conftest import make_pm_lm


def test_infer_provider_claude():
    assert infer_provider("claude-sonnet-4-20250514", "openai") == "anthropic"


def test_infer_provider_gpt():
    assert infer_provider("gpt-4o", "anthropic") == "openai"


def test_infer_provider_unknown_falls_back_to_default():
    assert infer_provider("mistral-large", "anthropic") == "anthropic"


def test_per_agent_lm_override_built(tmp_path):
    """A pm agent with a gpt-4o model under an lmstudio default still gets its
    own override LM, and infer_provider routes the gpt model to openai."""
    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    config.llm = LLMConfig(default_provider="lmstudio", default_model="local-model")
    config.agents = {"pm": AgentConfig(model="gpt-4o")}

    orch = Orchestrator(config)

    assert "pm" in orch._agent_lms
    pm_lm = orch._agent_lms["pm"]
    # dspy.LM stores its target in .model as "<provider>/<model>"
    assert "gpt-4o" in pm_lm.model


def test_config_context_reaches_prompt(tmp_path):
    """Operator-written agent context must appear in the LM's actual messages."""
    beads = Beads(tmp_path / "tasks.jsonl")
    agent_config = AgentConfig(
        model="local-model", settings={"context": "RECORRO-CONTEXT-MARKER"}
    )

    lm = make_pm_lm()
    dspy.configure(lm=lm)

    agent = PMAgent(beads, agent_config=agent_config)
    task = beads.create(
        title="Ship feature",
        description="Need it soon",
        assigned_agent="pm",
    )
    result = agent.execute(task)
    assert result.success

    # DummyLM records every call in .history; the prompt lives in messages.
    assert lm.history, "LM was never called"
    all_content = " ".join(
        m.get("content", "")
        for call in lm.history
        for m in call["messages"]
    )
    assert "RECORRO-CONTEXT-MARKER" in all_content


def test_agent_subtasks_are_quarantined_for_approval(tmp_path):
    """REGRESSION (Recorro subtask explosion, 2026-06-14): agent-proposed subtasks
    must be created as PENDING_APPROVAL and excluded from ready(), so a single
    worked task can never auto-spawn a self-sustaining tree of deeper work.
    Approving one promotes it back into the ready queue."""
    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    orch = Orchestrator(config)
    # Route execution through the globally-configured fake LM, not a real per-agent LM.
    orch._agent_lms = {}

    # A PM answer routed to "dev" with acceptance criteria makes PMAgent emit subtasks.
    dspy.configure(lm=make_pm_lm(assigned_to="dev"))

    parent = orch.beads.create(
        title="Build login", description="Need it soon", assigned_agent="pm"
    )

    completed = orch.work(limit=10)
    assert parent.id in {t.id for t in completed}

    children = [t for t in orch.beads.list() if t.parent_id == parent.id]
    assert children, "PM should have proposed subtasks"

    # The guard: spawned subtasks are quarantined, not workable.
    assert all(t.status == TaskStatus.PENDING_APPROVAL for t in children)
    ready_ids = {t.id for t in orch.beads.ready()}
    assert not any(c.id in ready_ids for c in children), (
        "pending_approval subtasks must NOT be ready — this is the anti-explosion guard"
    )

    # Approving one promotes it to pending so the operator can let it run.
    approved = orch.beads.approve(children[0].id)
    assert approved.status == TaskStatus.PENDING
    assert children[0].id in {t.id for t in orch.beads.ready()}


def test_acceptance_criteria_are_metadata_not_tasks(tmp_path):
    """ACs are a verification contract stored ON the task — never one-task-per-AC.
    A non-decomposed PM task produces exactly ONE handoff, carrying the ACs as
    metadata, no matter how many acceptance criteria there are."""
    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    orch = Orchestrator(config)
    orch._agent_lms = {}

    acs = ["Login works", "Errors surfaced", "Rate-limited", "Audit-logged"]
    dspy.configure(
        lm=make_pm_lm(assigned_to="dev", acceptance=acs, needs_decomposition=False)
    )
    parent = orch.beads.create(
        title="Ship auth", description="Need it", assigned_agent="pm"
    )
    orch.work(limit=10)

    # ACs live on the parent as verification metadata, not as work.
    refreshed = orch.beads.get(parent.id)
    assert refreshed.metadata.get("acceptance_criteria") == acs

    children = [t for t in orch.beads.list() if t.parent_id == parent.id]
    assert len(children) == 1, "4 ACs must NOT become 4 tasks — one handoff only"
    assert children[0].metadata.get("acceptance_criteria") == acs


def test_pm_decomposition_is_explicit_and_count_capped(tmp_path):
    """Subtasks only when needs_decomposition is True, and never more than
    MAX_SUBTASKS_PER_TASK in a single execution."""
    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    orch = Orchestrator(config)
    orch._agent_lms = {}

    seven = [f"work item {i}" for i in range(7)]
    dspy.configure(
        lm=make_pm_lm(assigned_to="dev", needs_decomposition=True, subtasks=seven)
    )
    parent = orch.beads.create(
        title="Big epic", description="x", assigned_agent="pm"
    )
    orch.work(limit=10)

    children = [t for t in orch.beads.list() if t.parent_id == parent.id]
    assert len(children) == MAX_SUBTASKS_PER_TASK, "7 proposed → capped at 5"
    assert all(t.status == TaskStatus.PENDING_APPROVAL for t in children)


def test_dev_agent_does_not_self_spawn(tmp_path):
    """No self-recursion: DevAgent records its breakdown but never creates
    dev→dev subtasks (the engine behind the runaway tree)."""
    beads = Beads(tmp_path / "tasks.jsonl")
    dev_reply = {
        "reasoning": "Planned the work.",
        "approach": "Refactor the module",
        "files_to_modify": ["a.py"],
        "subtasks": ["step 1", "step 2", "step 3"],
        "risks": [],
        "ready_to_implement": True,
    }
    dspy.configure(lm=DummyLM([dev_reply], reasoning=True))

    agent = DevAgent(beads)
    task = beads.create(title="Build X", description="...", assigned_agent="dev")
    result = agent.execute(task)

    assert result.success
    assert result.subtasks is None, "dev must not spawn dev→dev subtasks"
    assert result.output.get("proposed_breakdown") == ["step 1", "step 2", "step 3"]


def test_devops_agent_does_not_self_spawn(tmp_path):
    """No self-recursion: DevOpsAgent records remediation steps but never creates
    devops→devops subtasks."""
    beads = Beads(tmp_path / "tasks.jsonl")
    devops_reply = {
        "reasoning": "Diagnosed the incident.",
        "root_cause": "Disk full",
        "severity": "low",
        "remediation_steps": ["free disk", "add alert"],
        "prevention": ["monitor disk"],
        "can_auto_remediate": True,
    }
    dspy.configure(lm=DummyLM([devops_reply], reasoning=True))

    agent = DevOpsAgent(beads)
    task = beads.create(title="Incident", description="x", assigned_agent="devops")
    result = agent.execute(task)

    assert result.success
    assert result.subtasks is None, "devops must not spawn devops→devops subtasks"
    children = [t for t in beads.list() if t.parent_id == task.id]
    assert children == []
