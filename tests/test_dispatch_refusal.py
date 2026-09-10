"""The verbs that replace `status=BLOCKED`, and the properties it was buying.

P2a of `ai-tasks/embedded-plane/PLAN.md`. The runtime wrote BLOCKED at eight
sites to mean "could not be dispatched". The plane refuses that word: BLOCKED
there is DERIVED from unmet dependencies and never stored, and `report_result`
takes only terminal outcomes — measured, not assumed. Six of the eight sites
also fire before any claim, where the plane refuses a report for want of a
lease.

So the fact moves from status to a metadata record, and every property the
status was silently providing has to be re-established explicitly. Each test
below is one of those properties. They are the interesting ones precisely
because none of them is about the record itself: they are about what stops
happening when a bead cannot be run.
"""

from __future__ import annotations

import pytest

from agentco_harness.beads import DISPATCH_REFUSAL_KEY, Beads, TaskStatus


@pytest.fixture
def beads(tmp_path):
    return Beads(tmp_path / "tasks.jsonl")


def test_a_refused_bead_is_not_offered_again(beads):
    """The whole point. BLOCKED bought exactly one thing — `ready()` stopped
    returning it — and that is what must survive the move to metadata.

    Getting this wrong is silent in the worst way: the bead still looks
    ordinary, and the cycle picks it up, refuses it, and records the same
    refusal forever. That is the 2026-08-04 globexapp failure (24 beads, 11
    days, `errors` pinned non-zero) which `test_unassigned_bead_is_blocked_not_
    looped` exists for.
    """
    task = beads.create("undispatchable", "d", assigned_agent="dev")
    assert task.id in [t.id for t in beads.ready()]

    beads.refuse_dispatch(task.id, code="no_executor", message="nothing can run it")

    assert task.id not in [t.id for t in beads.ready()]
    assert beads.get(task.id).status == TaskStatus.PENDING


def test_clearing_the_refusal_returns_the_bead_to_the_ready_set(beads):
    """There has to be a way back, or a config fix leaves the bead parked."""
    task = beads.create("undispatchable", "d", assigned_agent="dev")
    beads.refuse_dispatch(task.id, code="no_provider", message="no provider")
    assert task.id not in [t.id for t in beads.ready()]

    beads.clear_dispatch_refusal(task.id)

    assert task.id in [t.id for t in beads.ready()]
    assert DISPATCH_REFUSAL_KEY not in beads.get(task.id).metadata


def test_the_refusal_records_why_not_just_that(beads):
    """A record that says only "refused" moves the silence rather than ending
    it. The operator's next action is in the message and the remediation."""
    task = beads.create("undispatchable", "d")
    beads.refuse_dispatch(
        task.id,
        code="egress_denied",
        message="egress denied: PUBLIC ceiling",
        remediation="Correct the bead's data class.",
    )

    refusal = beads.get(task.id).metadata[DISPATCH_REFUSAL_KEY]
    assert refusal["code"] == "egress_denied"
    assert "PUBLIC ceiling" in refusal["message"]
    assert refusal["remediation"] == "Correct the bead's data class."
    assert refusal["at"]


def test_refusing_dispatch_is_not_reported_as_a_failure(beads):
    """A routing refusal is not work that was tried and found wanting.

    The distinction is load-bearing now that outcomes feed an ASOP's
    `outcomes_by_version`: counting a missing credential as a failed attempt is
    the runtime marking work bad because it could not find a way to try it.
    """
    task = beads.create("undispatchable", "d")
    beads.refuse_dispatch(task.id, code="no_provider", message="no provider")

    after = beads.get(task.id)
    assert after.status is not TaskStatus.FAILED
    assert after.lease_attempt == 0, "a refusal must not burn an attempt"


def test_annotate_refuses_to_pretend_it_edits_dependencies(beads):
    """The one divergence of the six that does not announce itself.

    Measured against a real plane: `annotate({"blocked_by": [...]})` is
    accepted, writes a metadata key of that name, leaves the real dependency
    list untouched, and returns the item looking updated. `unmet_blockers`
    still returns the old list.

    A silent no-op here would be carried across the substitution instead of
    caught by it — a bead whose dependencies were "edited" and were not. So the
    runtime's own `annotate` refuses the key outright rather than reproducing
    the plane's behaviour faithfully. Fidelity to a trap is not fidelity.
    """
    blocker = beads.create("first", "d")
    task = beads.create("second", "d", blocked_by=[blocker.id])

    with pytest.raises(ValueError, match="does not edit dependencies"):
        beads.annotate(task.id, {"blocked_by": []})

    assert beads.get(task.id).blocked_by == [blocker.id]


def test_annotate_merges_rather_than_replaces(beads):
    """Metadata written by one subsystem must not be erased by another's note."""
    task = beads.create("t", "d", metadata={"company": "umbrella"})

    beads.annotate(task.id, {"note": "seen"})

    metadata = beads.get(task.id).metadata
    assert metadata["company"] == "umbrella"
    assert metadata["note"] == "seen"


def test_a_refused_bead_does_not_hold_the_cycle_at_baseline(tmp_path):
    """`_actionable_bead_count` excluded BLOCKED on purpose, and the beads it
    excluded are now PENDING.

    Missing this is invisible: the cycle simply never backs off, kept at
    baseline forever by beads nothing can run. The old docstring predicted it
    ("Blocked beads are excluded on purpose... they must not hold the adaptive
    interval at baseline"), which is the only reason it was caught.
    """
    from agentco_harness.config import Config
    from agentco_harness.orchestrator import Orchestrator

    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    orch = Orchestrator(config)
    task = orch.beads.create("undispatchable", "d", assigned_agent="dev")
    assert orch._actionable_bead_count() == 1

    orch.beads.refuse_dispatch(task.id, code="no_executor", message="nothing runs it")

    assert orch._actionable_bead_count() == 0
    assert orch._open_bead_count() == 1, "still unfinished work, and still visible"


def test_a_refused_bead_still_reaches_the_human_queue(tmp_path):
    """Excluded from `ready()` AND absent from `me` would be silently parked —
    the worst of both, and the failure that would falsify the reason this was
    chosen over a stored status."""
    from agentco_harness.config import Config
    from agentco_harness.me import _collect_instance
    from datetime import datetime, timezone

    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    beads = Beads(config.tasks_path)
    task = beads.create("undispatchable", "d", assigned_agent="dev")
    beads.refuse_dispatch(
        task.id, code="no_executor", message="no assigned_agent and no assigned_to"
    )

    items = _collect_instance(
        config, str(tmp_path / "config.yaml"), "test", 2, datetime.now(timezone.utc)
    )

    mine = [i for i in items if i.task_id == task.id]
    assert mine, "a refused bead vanished from the human queue"
    assert mine[0].kind == "blocked"
    assert "no assigned_agent" in mine[0].detail
