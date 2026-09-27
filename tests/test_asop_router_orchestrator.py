"""The router only ever runs from `observe()`, and only when config says so.

Uses the same fixtures/pattern as `tests/test_extension_seams.py` — a stub
source plus a stubbed classifier so no LM is needed for the classification
step itself; only the router's own call is under test here.
"""

from __future__ import annotations

import pytest

import agentco_harness.orchestrator as orchestrator_mod
from agentco_harness.config import AgentConfig, Config, LLMConfig
from agentco_harness.orchestrator import (
    SOURCE_FACTORIES,
    Orchestrator,
    register_source_factory,
)


@pytest.fixture(autouse=True)
def _clean_sources():
    saved = list(SOURCE_FACTORIES)
    SOURCE_FACTORIES.clear()
    yield
    SOURCE_FACTORIES[:] = saved


def _orch(tmp_path) -> Orchestrator:
    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    config.llm = LLMConfig(default_provider="lmstudio", default_model="local-model")
    config.agents = {"claude": AgentConfig(model="local-model")}
    config.notify.enabled = False
    return Orchestrator(config)


class _Event:
    source = "stub"
    source_id = "e1"
    content = "something happened"
    context = {}


class _Source:
    name = "stub"

    def poll(self):
        return [_Event()]


def _register_stub_source():
    register_source_factory(lambda config: [_Source()])


def test_router_is_not_called_when_disabled(tmp_path, monkeypatch):
    _register_stub_source()
    orch = _orch(tmp_path)
    orch.classifier.process = lambda **kw: orch.beads.create(
        title=kw["content"], description="", source=kw["source"]
    )

    def boom(*a, **kw):
        raise AssertionError("router must not run when disabled")

    monkeypatch.setattr(orchestrator_mod, "_asop_router_route", boom)

    assert orch.config.asop_router.enabled is False
    created = orch.observe()
    assert len(created) == 1


def test_router_is_called_per_created_task_when_enabled(tmp_path, monkeypatch):
    _register_stub_source()
    orch = _orch(tmp_path)
    orch.config.asop_router.enabled = True
    orch.classifier.process = lambda **kw: orch.beads.create(
        title=kw["content"], description="", source=kw["source"]
    )

    seen = []
    monkeypatch.setattr(
        orchestrator_mod, "_asop_router_route",
        lambda task, **kw: seen.append(task.id),
    )

    created = orch.observe()
    assert len(created) == 1
    assert seen == [created[0].id]


def test_a_router_bug_costs_one_event_not_the_cycle(tmp_path, monkeypatch):
    """Same isolation posture as the polling try/except one line up —
    `_route_to_asop` is not allowed to be the thing that makes `observe()`
    itself raise."""
    _register_stub_source()
    orch = _orch(tmp_path)
    orch.config.asop_router.enabled = True
    orch.classifier.process = lambda **kw: orch.beads.create(
        title=kw["content"], description="", source=kw["source"]
    )
    monkeypatch.setattr(
        orchestrator_mod, "_asop_router_route",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("router bug")),
    )

    created = orch.observe()  # must not raise
    assert len(created) == 1
