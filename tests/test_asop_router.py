"""ac-3de1dd9d: the second classification step, against ASOP.md's own store.

Three outcomes to prove, plus the two failure modes that must degrade safely
(a hallucinated or low-confidence match, and an LM that raises outright), and
the one design decision worth pinning explicitly: a MATCH that cannot
actually be filed (no configured binding for a role) leaves the bead exactly
as the classifier produced it — PENDING, unrouted — rather than half-doing
the redirect.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from asop import SopStatus

from agentco_harness.asop_router import candidates_log_path, route
from agentco_harness.asop_store import AsopStore
from agentco_harness.beads import Beads, TaskStatus
from agentco_harness.config import AsopRouterConfig


def widget_asop(input_names=("title",), role="worker"):
    return {
        "title": "Handle a widget request",
        "task_type": "widget",
        "purpose": "handle widget requests end to end",
        "inputs": [{"name": n} for n in input_names],
        "roles": {role: {"kind": "agent"}},
        "steps": [
            {"name": "do-it", "role": role, "purpose": "do the work",
             "gate": {"kind": "deterministic", "check": "true"}},
        ],
    }


def _active(store, asop_id="widget-flow", **kwargs):
    rec = store.create(widget_asop(**kwargs), author="m", author_kind="human", asop_id=asop_id)
    return store.activate(rec.asop_id, rec.version, by_kind="human")


def _stub(matched_asop_id="", confidence=0.0, reason=""):
    def predict(**kwargs):
        return SimpleNamespace(matched_asop_id=matched_asop_id, confidence=confidence, reason=reason)
    return predict


def _boom(**kwargs):
    raise RuntimeError("LM outage")


@pytest.fixture()
def store(tmp_path):
    return AsopStore(tmp_path / "asops.jsonl")


@pytest.fixture()
def beads(tmp_path):
    return Beads(tmp_path / "tasks.jsonl")


@pytest.fixture()
def cfg():
    return AsopRouterConfig(bindings={"worker": "claude"})


# --------------------------------------------------------------- off by default

def test_off_by_default():
    assert AsopRouterConfig().enabled is False


# --------------------------------------------------------------------- MATCH

def test_match_files_a_run_and_skips_the_plain_bead(store, beads, cfg):
    _active(store)
    task = beads.create(title="widget broke", description="d", metadata={"category": "widget"})

    result = route(
        task, store=store, beads=beads, config=cfg,
        predict=_stub("widget-flow", 0.9, "this is a widget-flow case"),
    )

    assert result.outcome == "match"
    assert result.run_id is not None
    refreshed = beads.get(task.id)
    assert refreshed.status == TaskStatus.SKIPPED
    assert refreshed.metadata["asop_route"] == {
        "outcome": "match", "reason": "this is a widget-flow case",
        "asop_id": "widget-flow", "version": 1, "confidence": 0.9,
        "run_id": result.run_id,
    }
    run_parent = beads.get(result.run_id)
    assert run_parent.metadata["sop_ref"] == {"asop_id": "widget-flow", "version": 1}
    children = [t for t in beads.list() if t.parent_id == result.run_id]
    assert len(children) == 1
    assert children[0].assigned_agent == "claude"


def test_match_with_no_binding_leaves_the_bead_pending(store, beads):
    _active(store)
    task = beads.create(title="widget broke", description="d", metadata={"category": "widget"})
    cfg = AsopRouterConfig(bindings={})  # 'worker' role has nothing configured

    result = route(task, store=store, beads=beads, config=cfg,
                    predict=_stub("widget-flow", 0.9, "fits"))

    assert result.outcome == "match"
    assert result.run_id is None
    assert "role" in result.run_error
    refreshed = beads.get(task.id)
    assert refreshed.status == TaskStatus.PENDING          # unchanged — never half-filed
    assert not [t for t in beads.list() if t.parent_id != task.id and t.id != task.id]


def test_match_with_undeclared_input_leaves_the_bead_pending(store, beads, cfg):
    _active(store, input_names=("requirement",))  # not derivable from a bead
    task = beads.create(title="widget broke", description="d", metadata={"category": "widget"})

    result = route(task, store=store, beads=beads, config=cfg,
                    predict=_stub("widget-flow", 0.9, "fits"))

    assert result.outcome == "match"
    assert result.run_id is None
    assert "input" in result.run_error
    assert beads.get(task.id).status == TaskStatus.PENDING


def test_a_hallucinated_asop_id_is_treated_as_no_match(store, beads, cfg):
    _active(store)
    task = beads.create(title="widget broke", description="d", metadata={"category": "widget"})

    result = route(task, store=store, beads=beads, config=cfg,
                    predict=_stub("not-a-real-asop", 0.99, "fits"))

    assert result.outcome != "match"
    assert beads.get(task.id).status == TaskStatus.PENDING


def test_below_threshold_is_no_match(store, beads, cfg):
    _active(store)
    task = beads.create(title="widget broke", description="d", metadata={"category": "widget"})

    result = route(task, store=store, beads=beads, config=cfg,
                    predict=_stub("widget-flow", 0.4, "maybe"))

    assert result.outcome != "match"
    assert beads.get(task.id).status == TaskStatus.PENDING


# --------------------------------------------------------------- LM failure

def test_lm_failure_degrades_to_plain_not_candidate(store, beads, cfg):
    _active(store)
    for _ in range(5):  # enough completed lookalikes to satisfy candidate on its own
        beads.create(title="widget broke", description="d", status=TaskStatus.DONE,
                      metadata={"category": "widget"})
    task = beads.create(title="widget broke", description="d", metadata={"category": "widget"})

    result = route(task, store=store, beads=beads, config=cfg, predict=_boom)

    assert result.outcome == "plain"
    assert "router error" in result.reason
    assert not candidates_log_path(store).exists()


# ------------------------------------------------------------------- PLAIN

def test_plain_when_nothing_matches_and_nothing_repeats(store, beads, cfg):
    task = beads.create(title="one-off widget thing", description="d",
                         metadata={"category": "widget"})

    result = route(task, store=store, beads=beads, config=cfg, predict=_stub())

    assert result.outcome == "plain"
    assert beads.get(task.id).status == TaskStatus.PENDING
    assert beads.get(task.id).metadata["asop_route"]["outcome"] == "plain"


# --------------------------------------------------------------- CANDIDATE

def test_candidate_when_enough_completed_beads_look_alike(store, beads, cfg):
    for _ in range(3):
        beads.create(title="reset the widget cache", description="d", status=TaskStatus.DONE,
                      metadata={"category": "widget"})
    task = beads.create(title="reset the widget cache", description="d",
                         metadata={"category": "widget"})

    result = route(task, store=store, beads=beads, config=cfg, predict=_stub())

    assert result.outcome == "candidate"
    assert result.similar_count == 3
    refreshed = beads.get(task.id)
    assert refreshed.status == TaskStatus.PENDING          # still a plain, dispatchable bead
    assert refreshed.metadata["asop_route"]["outcome"] == "candidate"
    lines = candidates_log_path(store).read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["task_id"] == task.id and entry["similar_count"] == 3


def test_candidate_needs_the_configured_minimum(store, beads, cfg):
    for _ in range(2):  # one short of the default minimum (3)
        beads.create(title="reset the widget cache", description="d", status=TaskStatus.DONE,
                      metadata={"category": "widget"})
    task = beads.create(title="reset the widget cache", description="d",
                         metadata={"category": "widget"})

    result = route(task, store=store, beads=beads, config=cfg, predict=_stub())

    assert result.outcome == "plain"
    assert not candidates_log_path(store).exists()


def test_different_category_does_not_count_toward_candidate(store, beads, cfg):
    for _ in range(3):
        beads.create(title="reset the widget cache", description="d", status=TaskStatus.DONE,
                      metadata={"category": "other"})
    task = beads.create(title="reset the widget cache", description="d",
                         metadata={"category": "widget"})

    result = route(task, store=store, beads=beads, config=cfg, predict=_stub())

    assert result.outcome == "plain"


# ---------------------------------------------------------- real dspy wiring

def test_routes_through_a_real_dspy_predictor(store, beads, cfg):
    """Not just the injectable stub — the default predictor path (`predict=None`,
    same DSPy plumbing `Classifier` uses) must also work end to end."""
    import dspy
    from dspy.utils.dummies import DummyLM

    _active(store)
    task = beads.create(title="widget broke", description="d", metadata={"category": "widget"})

    dspy.configure(lm=DummyLM(
        [{"reasoning": "matches widget-flow", "matched_asop_id": "widget-flow",
          "confidence": 0.95, "reason": "it is a widget-flow case"}],
        reasoning=True,
    ))

    result = route(task, store=store, beads=beads, config=cfg)

    assert result.outcome == "match"
    assert result.run_id is not None
