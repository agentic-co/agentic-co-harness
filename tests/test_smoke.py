"""End-to-end smoke test: drive a task through the orchestrator with a fake LM."""

from __future__ import annotations

import dspy

from agentco_harness.beads import Beads, TaskStatus
from agentco_harness.config import AgentConfig, Config, LLMConfig
from agentco_harness.orchestrator import Orchestrator

from .conftest import make_pm_lm


def _build_config(tmp_path) -> Config:
    """A Config whose tasks live in tmp_path and whose provider needs no key."""
    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    # lmstudio provider builds a dspy.LM with a dummy api_key — no real key.
    config.llm = LLMConfig(default_provider="lmstudio", default_model="local-model")
    # Trim agents to just pm so _setup_dspy builds no anthropic/openai LMs
    # that would otherwise need keys for overrides.
    config.agents = {"pm": AgentConfig(model="local-model")}
    return config


def test_smoke_task_runs_to_done_and_spawns_subtask(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = _build_config(tmp_path)

    orch = Orchestrator(config)

    # Replace the configured LM with a DummyLM so no network call happens.
    dspy.configure(lm=make_pm_lm())

    beads = orch.beads
    task = beads.create(
        title="Add password reset",
        description="Customers cannot reset passwords",
        assigned_agent="pm",
    )

    completed = orch.work()

    assert task in completed or any(t.id == task.id for t in completed)
    refreshed = beads.get(task.id)
    assert refreshed.status == TaskStatus.DONE

    # PM routed to "dev" -> a subtask should now exist with parent_id == task.id
    children = [t for t in beads.list() if t.parent_id == task.id]
    assert len(children) == 1
    assert children[0].assigned_agent == "dev"


def test_smoke_heartbeat_recorded(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = _build_config(tmp_path)

    orch = Orchestrator(config)
    dspy.configure(lm=make_pm_lm())

    orch.beads.create(
        title="Investigate latency",
        description="P99 climbing",
        assigned_agent="pm",
    )
    orch.work()

    status = orch.status()
    assert status["last_work_at"] is not None
    assert status["done"] >= 1
