"""The Harness runs beads; everything else plugs in.

v1 hard-wired three personal pipelines into the cycle: a nightly `retro` task
type, a feeds watermark advanced on completion, and a set of polled sources.
The Harness replaces each with a registry. These tests pin the contract an
extension (the LifeOS pack, a company integration) relies on:

* a registered cycle handler owns its task type end to end;
* an unknown task type still takes the normal executor path (no silent skip);
* completion hooks fire once per DONE task and never crash the cycle;
* with no source factory registered, observe() is a heartbeat-only no-op.
"""

from __future__ import annotations

import json

import pytest

import agentco_harness.orchestrator as orchestrator_mod
from agentco_harness.beads import TaskStatus
from agentco_harness.config import AgentConfig, Config, LLMConfig
from agentco_harness.executor import ExecResult
from agentco_harness.orchestrator import (
    COMPLETION_HOOKS,
    CYCLE_HANDLERS,
    SOURCE_FACTORIES,
    Orchestrator,
    register_completion_hook,
    register_cycle_handler,
    register_source_factory,
)


@pytest.fixture(autouse=True)
def _clean_registries():
    saved = (dict(CYCLE_HANDLERS), list(COMPLETION_HOOKS), list(SOURCE_FACTORIES))
    CYCLE_HANDLERS.clear()
    COMPLETION_HOOKS.clear()
    SOURCE_FACTORIES.clear()
    yield
    CYCLE_HANDLERS.clear()
    CYCLE_HANDLERS.update(saved[0])
    COMPLETION_HOOKS[:] = saved[1]
    SOURCE_FACTORIES[:] = saved[2]


def _orch(tmp_path) -> Orchestrator:
    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    config.llm = LLMConfig(default_provider="lmstudio", default_model="local-model")
    config.agents = {"claude": AgentConfig(model="local-model")}
    config.notify.enabled = False
    return Orchestrator(config)


def test_registered_handler_owns_its_task_type(tmp_path):
    orch = _orch(tmp_path)
    seen = []

    def handle(o, task, now):
        seen.append(task.id)
        o.beads.update(task.id, status=TaskStatus.DONE, result="handled")
        return True

    register_cycle_handler("nightly_thing", handle)
    task = orch.beads.create(title="t", description="", metadata={"type": "nightly_thing"})

    assert orch._execute_cycle_task(task) is True
    assert seen == [task.id]
    assert orch.beads.get(task.id).status == TaskStatus.DONE


def test_unregistered_type_falls_through_to_the_executor(tmp_path, monkeypatch):
    """No handler must never mean 'quietly drop it' — the bead takes the
    ordinary agent path (here: the claude subprocess, faked)."""
    orch = _orch(tmp_path)
    calls = []

    def fake(prompt, timeout, max_turns, model=None):
        calls.append(prompt)
        return ExecResult(True, json.dumps({"ok": True}), None, 0, 0.1)

    monkeypatch.setattr(orchestrator_mod, "run_claude_task", fake)
    task = orch.beads.create(
        title="t", description="", assigned_agent="claude", metadata={"type": "retro"}
    )
    orch._execute_cycle_task(task)
    assert calls, "a task type with no registered handler must still be dispatched"


def test_completion_hooks_fire_and_are_isolated(tmp_path):
    orch = _orch(tmp_path)
    fired = []

    def boom(o, task):
        raise RuntimeError("extension bug")

    def record(o, task):
        fired.append(task.id)

    register_completion_hook(boom)
    register_completion_hook(record)
    task = orch.beads.create(title="t", description="")

    orch._run_completion_hooks(task)  # must not raise
    assert fired == [task.id]


def test_observe_is_a_no_op_without_sources(tmp_path):
    orch = _orch(tmp_path)
    assert orch.observe() == []
    assert orch.beads.list() == []


def test_registered_source_creates_tasks(tmp_path):
    class _Event:
        source = "stub"
        source_id = "e1"
        content = "something happened"
        context = {}

    class _Source:
        name = "stub"

        def poll(self):
            return [_Event()]

    register_source_factory(lambda config: [_Source()])
    orch = _orch(tmp_path)
    # The classifier is the DSPy triage step; stub it so the test needs no LM.
    orch.classifier.process = lambda **kw: orch.beads.create(
        title=kw["content"], description="", source=kw["source"]
    )
    created = orch.observe()
    assert len(created) == 1
    assert created[0].source == "stub"
    assert orch.beads.get(created[0].id).title == "something happened"
