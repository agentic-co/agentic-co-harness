"""The executor backend seam: what an ASOP role binding names must be
something this runtime can execute, and registering one is enough."""

from __future__ import annotations

import pytest

from agentco_harness import backends, egress
from agentco_harness.beads import Beads, TaskStatus
from agentco_harness.config import Config
from agentco_harness.orchestrator import SPECIAL_EXECUTORS, Orchestrator


@pytest.fixture(autouse=True)
def _restore_registry():
    saved = dict(backends.EXECUTOR_BACKENDS)
    routes = dict(egress.AGENT_ROUTE)
    yield
    backends.EXECUTOR_BACKENDS.clear(); backends.EXECUTOR_BACKENDS.update(saved)
    egress.AGENT_ROUTE.clear(); egress.AGENT_ROUTE.update(routes)


def test_the_four_v1_executors_are_registered_built_ins():
    assert {"planner", "claude", "zai", "forge"} <= backends.executor_names()
    assert backends.resolve("forge").route == "FORGE"
    assert backends.resolve("planner").egress_checked is False


def test_special_executors_is_a_live_view_of_the_registry():
    assert "forge" in SPECIAL_EXECUTORS
    assert "echo" not in SPECIAL_EXECUTORS
    backends.register_executor_backend("echo", lambda o, t: True, route="NATIVE")
    assert "echo" in SPECIAL_EXECUTORS
    assert "echo" in set(SPECIAL_EXECUTORS)


def test_registering_declares_the_egress_route():
    assert "echo" not in egress.AGENT_ROUTE
    backends.register_executor_backend("echo", lambda o, t: True, route="NATIVE")
    assert egress.AGENT_ROUTE["echo"] == "NATIVE"


def test_a_registered_backend_is_dispatched_and_owns_completion(tmp_path):
    seen = []

    def execute(orch, task):
        orch.beads.claim(task.id, "echo")
        orch.beads.complete(task.id, result="echoed")
        seen.append(task.id)
        return True

    backends.register_executor_backend("echo", execute, route="NATIVE", egress_checked=False)
    config = Config(); config.tasks_path = str(tmp_path / "tasks.jsonl")
    beads = Beads(config.tasks_path)
    task = beads.create("say it", "d", assigned_agent="echo")
    orch = Orchestrator(config)
    assert orch._execute_cycle_task(beads.get(task.id)) is True
    assert seen == [task.id]
    assert beads.get(task.id).status is TaskStatus.DONE


def test_an_unregistered_name_still_takes_the_ordinary_agent_path(tmp_path, monkeypatch):
    config = Config(); config.tasks_path = str(tmp_path / "tasks.jsonl")
    beads = Beads(config.tasks_path)
    task = beads.create("x", "d", assigned_agent="dev")
    orch = Orchestrator(config)
    called = []
    monkeypatch.setattr(orch, "_execute_task", lambda t: called.append(t.id) or True)
    assert orch._execute_cycle_task(beads.get(task.id)) is True
    assert called == [task.id]


def test_a_backend_needs_a_name_and_a_route():
    with pytest.raises(ValueError):
        backends.register_executor_backend("", lambda o, t: True, route="NATIVE")
    with pytest.raises(ValueError):
        backends.register_executor_backend("x", lambda o, t: True, route="")


def test_an_asop_binding_to_a_registered_backend_files_a_dispatchable_bead(tmp_path):
    """The seam and the store meet here: bind a role to a backend, and the
    step bead it files is one the cycle knows how to run."""
    from agentco_harness.asop_store import AsopStore
    backends.register_executor_backend("echo", lambda o, t: True, route="NATIVE", egress_checked=False)
    store = AsopStore(tmp_path / "asops.jsonl")
    beads = Beads(tmp_path / "tasks.jsonl")
    store.create({"title": "T", "roles": {"r": {"kind": "agent"}},
                  "steps": [{"name": "s", "role": "r", "purpose": "p",
                             "gate": {"kind": "deterministic", "check": "true"}}]},
                 author="m", author_kind="human", asop_id="t")
    store.activate("t", 1, by_kind="human")
    parent = store.run("t", inputs={}, bindings={"r": "echo"}, beads=beads)
    step = next(t for t in beads.list() if t.parent_id == parent.id)
    assert step.assigned_agent == "echo"
    assert backends.resolve(step.assigned_agent) is not None
