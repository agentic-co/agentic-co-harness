"""The verify gate is a property of the TERMINAL STATUS, not of the verb used.

N9 (ai-tasks/unified/DECISIONS.md, principal's call 2026-09-16). The invariant
inherited from the PoC's PPEV work was *"every route to DONE funnels through one
choke point"*. It held to the letter and failed in spirit, because the successor
grew terminal states the original rule never contemplated: ``decline(terminal=
True)`` reaches SKIPPED and ``approve reject`` reaches SKIPPED, neither
consulting ``metadata.verify``, while ``retire()`` — which means the same thing —
refuses a gated bead outright.

The fix is RELOCATION, not three patches. ``retire()``'s refusal moves into
``update()`` next to the DONE check, keyed on the terminal status being written:

    DONE           gate must pass          (unchanged)
    VERIFY_FAILED  exempt                  (it is the gate's own output)
    SKIPPED        refused if gated        (retire()'s rule, relocated)
    CANCELLED      allowed                 (D2: the opposite of a completion claim)
    FAILED         allowed                 (a failure is not a completion claim)

This is the same move ``_approval_answers_gate`` already made and documented in
its own docstring: *"Not defensive duplication — relocation: these are the
conditions, and ``approve_verify`` is now one convenient way to satisfy them
rather than the place they live."* ``retire()`` keeps its check for the better
error message; it is no longer the place the rule lives.

WHAT THESE TESTS ARE FOR. ``test_terminal_paths.py`` asserts what the set of
doors IS. This file asserts what happens when you walk through one carrying a
gate — so the two SKIPPED doors cannot reopen by being rewritten, moved, or
duplicated somewhere new. Every test here failed before the relocation.
"""

from __future__ import annotations

import pytest

from agentco_harness import humans
from agentco_harness.beads import Beads, TaskStatus

GATE = {"class": "judged", "check": "is this actually done?"}


@pytest.fixture
def beads(tmp_path):
    return Beads(tmp_path / "tasks.jsonl")


@pytest.fixture
def gated(beads):
    """A bead carrying a verify gate, sitting PENDING — the early case.

    Deliberately NOT parked at AWAITING_VERIFY. N5's observation was that the
    status tuple guarding ``decline`` excludes a parked bead and so catches the
    late case while missing the early one — and a gate still AHEAD of the work
    is exactly the gate worth enforcing.
    """
    return beads.create("migrate", "d", metadata={"verify": dict(GATE)})


@pytest.fixture
def ungated(beads):
    return beads.create("clean up the scratch dir", "d")


# ------------------------------------------------- the doors, walked with a gate


def test_raw_update_to_skipped_is_refused_on_a_gated_bead(beads, gated):
    """The choke point itself, reached directly.

    Every other test in this file is a caller of this one path. If this passes
    and one of those fails, the caller grew its own way to a terminal status —
    which is the thing the relocation exists to make impossible.
    """
    with pytest.raises(ValueError, match="verify gate"):
        beads.update(gated.id, status=TaskStatus.SKIPPED)
    assert beads.get(gated.id).status is TaskStatus.PENDING


def test_terminal_decline_is_refused_on_a_gated_bead(beads, gated):
    """N5's door — ``humans.decline_task(terminal=True)``, unedited.

    The point of the relocation is that this file was never touched: the door
    closes because SKIPPED closed, not because decline() learned a new rule.
    """
    beads.update(gated.id, assigned_to="human:alex")
    with pytest.raises(ValueError, match="verify gate"):
        humans.decline_task(beads, gated.id, "kill-dated", terminal=True)
    assert beads.get(gated.id).status is TaskStatus.PENDING


def test_non_terminal_decline_still_works_on_a_gated_bead(beads, gated):
    """The common case must not become collateral damage.

    ``terminal=False`` returns a bead to the queue at PENDING. That is not a
    terminal status and has nothing to do with the gate, so it must keep
    working — a relocation that broke it would be enforcing the rule on the
    wrong event.
    """
    beads.update(gated.id, assigned_to="human:alex")
    returned = humans.decline_task(beads, gated.id, "not my area")
    assert returned.status is TaskStatus.PENDING
    assert returned.assigned_to is None


def test_approve_reject_is_refused_on_a_gated_bead(beads, gated):
    """``cli.py``'s ``approve reject`` — retire()'s job written as a raw update().

    A bead at PENDING_APPROVAL has never run, so its gate is entirely ahead of
    it. Rejecting it to SKIPPED was the purest instance of the N9 defect: the
    same semantics as ``retire()``, without ``retire()``'s guard.
    """
    beads.update(gated.id, status=TaskStatus.PENDING_APPROVAL)
    with pytest.raises(ValueError, match="verify gate"):
        beads.update(gated.id, status=TaskStatus.SKIPPED, result="Rejected by principal")
    assert beads.get(gated.id).status is TaskStatus.PENDING_APPROVAL


# ------------------------------------------------------- what must NOT change


def test_cancel_still_abandons_gated_work(beads, gated):
    """D2, and the reason this is a policy table rather than a blanket rule.

    CANCELLED is the sanctioned way past a gate. If the relocation had closed
    this too, the only remaining exit for a gated bead nobody will finish would
    be to leave it live forever — which is the state D1 was written about.
    """
    cancelled = beads.cancel(gated.id, by="alex", reason="requirement withdrawn")
    assert cancelled.status is TaskStatus.CANCELLED
    assert cancelled.metadata["cancellation"]["by"] == "alex"


def test_fail_still_works_on_a_gated_bead(beads, gated):
    """A failure is not a completion claim, so the gate has no business here."""
    failed = beads.fail(gated.id, result="the migration blew up")
    assert failed.status is TaskStatus.FAILED


def test_reject_verify_still_reaches_verify_failed(beads, monkeypatch):
    """VERIFY_FAILED is the gate's OWN output — gating it would deadlock it.

    This is the exemption that makes the table a table. A blanket "no terminal
    status on a gated bead" rule would make a rejected gate unable to record
    that it was rejected.
    """
    monkeypatch.setenv("ASOP_VERIFIERS", "verifier-b")
    task = beads.create("migrate", "d", metadata={"verify": dict(GATE)})
    claimed = beads.claim(task.id, "executor-a")
    beads.report_result(task.id, claimed.lease_attempt, TaskStatus.DONE, result="done")
    assert beads.get(task.id).status is TaskStatus.AWAITING_VERIFY

    beads.reject_verify(task.id, approver="verifier-b", reason="not actually migrated")
    assert beads.get(task.id).status is TaskStatus.VERIFY_FAILED


def test_ungated_work_still_reaches_skipped_by_every_route(beads, ungated):
    """The rule is about GATED beads. Ungated administrative close is untouched."""
    beads.update(ungated.id, assigned_to="human:alex")
    declined = humans.decline_task(beads, ungated.id, "kill-dated", terminal=True)
    assert declined.status is TaskStatus.SKIPPED

    second = beads.create("another moot thing", "d")
    retired = beads.retire(second.id, by="alex", reason="moot")
    assert retired.status is TaskStatus.SKIPPED


def test_retire_still_refuses_first_with_its_own_message(beads, gated):
    """Relocation keeps the better error, it does not delete it.

    ``retire()`` names ``cancel`` as the way out; the choke point cannot, because
    it does not know which verb the caller reached for. Losing that message
    would make the rule correct and unhelpable.
    """
    with pytest.raises(ValueError, match="Use `cancel`"):
        beads.retire(gated.id, by="alex", reason="moot")
