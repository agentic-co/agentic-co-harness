"""The "inverse shadow" intake gate (`intake.require_approval`, principal
2026-09-25): nothing a Classifier-created bead reaches on its own runs
without a human approving it first.

Three layers, each with its own test group below:

* `Classifier` itself — born PENDING_APPROVAL, carrying `requires_approval`,
  when the flag is on; unchanged when it is off.
* the ASOP router — a MATCH is decided and recorded but never FILED while
  approval is held (`held: true`); CANDIDATE/PLAIN are unaffected.
* `finalize_held_match` — the other half, run at `approve task` time.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentco_harness.asop_router import finalize_held_match, route
from agentco_harness.asop_store import AsopStore
from agentco_harness.beads import Beads, TaskStatus
from agentco_harness.config import AsopRouterConfig, Config, IntakeConfig


def widget_asop(role="worker"):
    return {
        "title": "Handle a widget request",
        "task_type": "widget",
        "purpose": "handle widget requests end to end",
        "inputs": [{"name": "title"}],
        "roles": {role: {"kind": "agent"}},
        "steps": [
            {"name": "do-it", "role": role, "purpose": "do the work",
             "gate": {"kind": "deterministic", "check": "true"}},
        ],
    }


def _active(store, asop_id="widget-flow"):
    rec = store.create(widget_asop(), author="m", author_kind="human", asop_id=asop_id)
    return store.activate(rec.asop_id, rec.version, by_kind="human")


def _stub(matched_asop_id="", confidence=0.0, reason=""):
    def predict(**kwargs):
        return SimpleNamespace(matched_asop_id=matched_asop_id, confidence=confidence, reason=reason)
    return predict


@pytest.fixture()
def store(tmp_path):
    return AsopStore(tmp_path / "asops.jsonl")


@pytest.fixture()
def beads(tmp_path):
    return Beads(tmp_path / "tasks.jsonl")


@pytest.fixture()
def cfg():
    return AsopRouterConfig(bindings={"worker": "claude"})


# ------------------------------------------------------------------- config

def test_require_approval_off_by_default():
    assert IntakeConfig().require_approval is False


def test_config_loads_intake_block(tmp_path):
    import yaml
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"tasks_path": "t.jsonl", "intake": {"require_approval": True}}))
    assert Config.load(str(cfg_path)).intake.require_approval is True


# -------------------------------------------------------------- Classifier

def test_classifier_holds_a_bead_for_approval(beads):
    from agentco_harness.agents import Classifier

    classifier = Classifier(beads, require_approval=True)
    classifier.classify = lambda **kw: SimpleNamespace(
        should_create_task=True, title="t", description="d", priority=2,
        assigned_agent="claude", category="bug",
    )
    task = classifier.process(source="stub", content="x", context="", source_id="1")

    assert task.status == TaskStatus.PENDING_APPROVAL
    assert task.metadata["requires_approval"] is True
    assert beads.ready() == []  # never dispatchable without approval


def test_classifier_default_is_unchanged(beads):
    from agentco_harness.agents import Classifier

    classifier = Classifier(beads)  # require_approval defaults False
    classifier.classify = lambda **kw: SimpleNamespace(
        should_create_task=True, title="t", description="d", priority=2,
        assigned_agent="claude", category="bug",
    )
    task = classifier.process(source="stub", content="x", context="", source_id="1")

    assert task.status == TaskStatus.PENDING
    assert "requires_approval" not in task.metadata


# --------------------------------------------------------------- the router

def test_a_match_is_held_not_filed_under_require_approval(store, beads, cfg):
    _active(store)
    task = beads.create(title="widget broke", description="d", status=TaskStatus.PENDING_APPROVAL,
                         metadata={"category": "widget", "requires_approval": True})

    result = route(task, store=store, beads=beads, config=cfg, require_approval=True,
                    predict=_stub("widget-flow", 0.9, "fits"))

    assert result.outcome == "match"
    assert result.held is True
    assert result.run_id is None
    refreshed = beads.get(task.id)
    assert refreshed.status == TaskStatus.PENDING_APPROVAL  # untouched — no retire happened
    assert refreshed.metadata["asop_route"]["held"] is True
    assert refreshed.metadata["asop_route"]["asop_id"] == "widget-flow"
    assert "run_id" not in refreshed.metadata["asop_route"]
    # No run was filed — no step beads exist anywhere.
    assert [t for t in beads.list() if t.parent_id] == []


def test_candidate_and_plain_are_unaffected_by_require_approval(store, beads, cfg):
    for _ in range(3):
        beads.create(title="reset the widget cache", description="d", status=TaskStatus.DONE,
                      metadata={"category": "widget"})
    task = beads.create(title="reset the widget cache", description="d",
                         status=TaskStatus.PENDING_APPROVAL, metadata={"category": "widget"})

    result = route(task, store=store, beads=beads, config=cfg, require_approval=True, predict=_stub())

    assert result.outcome == "candidate"
    assert beads.get(task.id).status == TaskStatus.PENDING_APPROVAL


# ------------------------------------------------------------ finalize_held_match

def test_finalize_files_a_held_match_and_retires_the_bead(store, beads, cfg):
    _active(store)
    task = beads.create(title="widget broke", description="d", status=TaskStatus.PENDING_APPROVAL,
                         metadata={"category": "widget", "requires_approval": True})
    route(task, store=store, beads=beads, config=cfg, require_approval=True,
          predict=_stub("widget-flow", 0.9, "fits"))
    held_task = beads.get(task.id)
    assert held_task.metadata["asop_route"]["held"] is True

    result = finalize_held_match(held_task, store=store, beads=beads, config=cfg)

    assert result is not None
    assert result.run_id is not None
    refreshed = beads.get(task.id)
    assert refreshed.status == TaskStatus.SKIPPED
    assert refreshed.metadata["asop_route"]["run_id"] == result.run_id
    assert "held" not in refreshed.metadata["asop_route"]
    children = [t for t in beads.list() if t.parent_id == result.run_id]
    assert len(children) == 1


def test_finalize_reports_an_unfilable_match_and_leaves_the_bead_pending_approval(store, beads):
    _active(store)
    cfg_no_bindings = AsopRouterConfig(bindings={})
    task = beads.create(title="widget broke", description="d", status=TaskStatus.PENDING_APPROVAL,
                         metadata={"category": "widget", "requires_approval": True})
    route(task, store=store, beads=beads, config=cfg_no_bindings, require_approval=True,
          predict=_stub("widget-flow", 0.9, "fits"))

    result = finalize_held_match(beads.get(task.id), store=store, beads=beads, config=cfg_no_bindings)

    assert result is not None
    assert result.run_id is None
    assert "role" in result.run_error
    assert beads.get(task.id).status == TaskStatus.PENDING_APPROVAL


def test_finalize_is_a_noop_when_nothing_was_held(store, beads, cfg):
    task = beads.create(title="one-off", description="d", status=TaskStatus.PENDING_APPROVAL,
                         metadata={"category": "widget"})
    assert finalize_held_match(task, store=store, beads=beads, config=cfg) is None
    assert beads.get(task.id).status == TaskStatus.PENDING_APPROVAL


def test_finalize_is_a_noop_when_not_pending_approval(store, beads, cfg):
    _active(store)
    task = beads.create(title="widget broke", description="d",
                         metadata={"category": "widget", "asop_route": {"outcome": "match", "held": True,
                                                                          "asop_id": "widget-flow", "version": 1}})
    # status is PENDING, not PENDING_APPROVAL — held or not, this is not finalize's business
    assert finalize_held_match(task, store=store, beads=beads, config=cfg) is None
