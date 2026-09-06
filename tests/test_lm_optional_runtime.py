"""A runtime without the optional LM extra must still run.

`_lm.py` exists to make DSPy optional, and its own refusal text says "the
runtime does not need it to run headless agent CLIs". The Orchestrator's
constructor demanded it anyway, so `agentic-co status` — which only reads
counts — crashed on a clean install. Found 2026-09-06 by pointing a freshly
installed runtime at a live node.
"""

from __future__ import annotations

import pytest

from agentco_harness import _lm
from agentco_harness.config import Config
from agentco_harness.orchestrator import Orchestrator


@pytest.fixture
def no_lm(monkeypatch):
    """An install with no lm extra: importing the layer raises, as it would."""
    def boom(feature="x"):
        raise _lm.LmUnavailable(feature)

    monkeypatch.setattr(_lm, "available", lambda: False)
    monkeypatch.setattr(_lm, "agents", boom)
    monkeypatch.setattr(_lm, "dspy", boom)
    monkeypatch.setattr(_lm, "triage", boom)
    monkeypatch.setattr(_lm, "optimize", boom)


def _orch(tmp_path):
    cfg = Config()
    cfg.tasks_path = str(tmp_path / "tasks.jsonl")
    cfg.config_path = str(tmp_path / "config.yaml")
    return Orchestrator(cfg)


def test_the_orchestrator_builds_without_the_lm_layer(tmp_path, no_lm):
    """The constructor must not need what only one code path uses."""
    orch = _orch(tmp_path)
    assert orch is not None


def test_reading_the_queue_works_without_it(tmp_path, no_lm):
    """`status` is the command that crashed. It only counts beads."""
    orch = _orch(tmp_path)
    orch.beads.create("a bead", "")
    assert len(orch.beads.list()) == 1


def test_executing_a_bead_works_without_it(tmp_path, no_lm, monkeypatch):
    """Dispatching to a headless CLI is exactly what the refusal text promises."""
    orch = _orch(tmp_path)
    t = orch.beads.create("do it", "", assigned_agent="lmstudio", requires=["text"])
    from agentco_harness import backends

    assert orch._capability_gap_ok(t, backends.resolve("lmstudio")) is True


def test_the_classifier_still_raises_for_the_caller_that_needs_it(tmp_path, no_lm):
    """Lazy, not silently absent: a node that polls sources still gets told."""
    orch = _orch(tmp_path)
    with pytest.raises(_lm.LmUnavailable):
        _ = orch.classifier


def test_it_is_built_once_and_reused(tmp_path, monkeypatch):
    """The property must not construct a new classifier on every access.

    monkeypatch, not assignment: `_lm` is a module every test shares, and an
    unrestored patch on it leaked into five unrelated tests the first time this
    was written — visible only in a full run, since each passed alone.
    """
    calls = []

    class FakeClassifier:
        def __init__(self, beads):
            calls.append(beads)

    class FakeAgents:
        Classifier = FakeClassifier

    orch = _orch(tmp_path)
    orch._classifier = None
    monkeypatch.setattr(_lm, "agents", lambda feature: FakeAgents)
    first, second = orch.classifier, orch.classifier
    assert first is second and len(calls) == 1
