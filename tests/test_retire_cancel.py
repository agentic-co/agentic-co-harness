"""`retire` and `cancel` — two verbs where the plan used to have one.

D2 (ai-tasks/unified/phase-0.md, decided by the principal 2026-09-15) resolves
a contradiction: `retire` was described elsewhere as the administrative-close
path for moot work "which can be gated" — but closing gated work that way lets
it go quiet without ever facing its gate, the same shape as the sanctioned
bypass this runtime already removed once (`update()`'s retired
``verify_gate=False``). The fix is two verbs instead of one meaning stretched
to cover both:

- ``retire``  → SKIPPED. Ungated administrative close only; refuses anything
  carrying a ``metadata.verify`` spec.
- ``cancel``  → CANCELLED (new terminal status, not DONE, not SKIPPED).
  Abandons work — gated or not — unfinished. Records who/why, produces no
  attestation, and the executor may not cancel its own work (mirrors
  ``approve_verify``'s separation).

The tests worth having are the ones that fail if either verb quietly slides
back into doing what the other one is for.
"""

from __future__ import annotations

import pytest

from agentco_harness.beads import Beads, TaskStatus

GATE = {"class": "judged", "check": "is this actually done?"}


@pytest.fixture
def beads(tmp_path):
    return Beads(tmp_path / "tasks.jsonl")


@pytest.fixture
def ungated(beads):
    """A plain, ungated bead sitting PENDING — the case retire exists for."""
    return beads.create("clean up the scratch dir", "d")


@pytest.fixture
def gated_pending(beads):
    """A bead carrying a verify gate, not yet claimed."""
    return beads.create("migrate", "d", metadata={"verify": dict(GATE)})


@pytest.fixture
def gated_parked(beads):
    """A gated bead actually parked at AWAITING_VERIFY — the honest way."""
    task = beads.create("migrate", "d", metadata={"verify": dict(GATE)})
    claimed = beads.claim(task.id, "executor-a")
    beads.report_result(task.id, claimed.lease_attempt, TaskStatus.DONE, result="done")
    assert beads.get(task.id).status is TaskStatus.AWAITING_VERIFY
    return beads.get(task.id)


# --------------------------------------------------------------------- retire


def test_retire_closes_ungated_work_as_skipped(beads, ungated):
    retired = beads.retire(ungated.id, by="alex", reason="no longer needed")

    assert retired.status is TaskStatus.SKIPPED
    assert retired.metadata["retirement"]["by"] == "alex"
    assert retired.metadata["retirement"]["reason"] == "no longer needed"


def test_retire_never_reaches_done(beads, ungated):
    """The whole point: retire is not an attestation and never asserts completion."""
    retired = beads.retire(ungated.id, by="alex")
    assert retired.status is not TaskStatus.DONE


def test_retire_refuses_a_gated_item_not_yet_parked(beads, gated_pending):
    """D2's headline behavior: a bead carrying a gate spec is refused outright,
    even before it has reached the gate — retiring it would mean it never does."""
    with pytest.raises(ValueError, match="carries a verify gate"):
        beads.retire(gated_pending.id, by="alex")

    assert beads.get(gated_pending.id).status is TaskStatus.PENDING


def test_retire_refuses_a_gated_item_parked_at_the_gate(beads, gated_parked):
    """This is the exact case embedded-plane's contradiction was about:
    'retire is the administrative-close path for moot work — which can be
    gated'. D2 overrules that: parked-and-gated is refused, not retired."""
    with pytest.raises(ValueError, match="carries a verify gate"):
        beads.retire(gated_parked.id, by="alex")

    assert beads.get(gated_parked.id).status is TaskStatus.AWAITING_VERIFY


def test_retire_refuses_without_a_named_retirer(beads, ungated):
    with pytest.raises(ValueError, match="who is retiring it"):
        beads.retire(ungated.id, by="")


def test_retire_refuses_an_already_terminal_bead(beads, ungated):
    beads.retire(ungated.id, by="alex")
    with pytest.raises(ValueError, match="already skipped"):
        beads.retire(ungated.id, by="alex")


def test_retire_on_a_missing_task_returns_none(beads):
    assert beads.retire("ac-doesnotexist", by="alex") is None


# --------------------------------------------------------------------- cancel


def test_cancel_reaches_a_terminal_status_that_is_not_done(beads, gated_parked):
    cancelled = beads.cancel(gated_parked.id, by="alex", reason="requirement dropped")

    assert cancelled.status is TaskStatus.CANCELLED
    assert cancelled.status is not TaskStatus.DONE
    assert cancelled.status is not TaskStatus.SKIPPED


def test_cancel_records_who_and_why(beads, gated_parked):
    cancelled = beads.cancel(gated_parked.id, by="alex", reason="requirement dropped")

    record = cancelled.metadata["cancellation"]
    assert record["by"] == "alex"
    assert record["reason"] == "requirement dropped"
    assert record["at"]


def test_cancel_produces_no_attestation(beads, gated_parked):
    """Cancelling is not a verdict on the work — nothing here should look like
    the ``verify_result``/``verify_approval`` shape an actual attestation has."""
    cancelled = beads.cancel(gated_parked.id, by="alex", reason="requirement dropped")

    assert "verify_approval" not in cancelled.metadata
    # the pre-existing unverified verify_result from the parked fixture must not
    # have been rewritten to look like a passed attestation
    assert cancelled.metadata.get("verify_result", {}).get("passed") is not True


def test_cancel_works_on_gated_work_that_retire_would_refuse(beads, gated_parked):
    """The whole reason cancel exists: it is the verb for exactly the case
    retire refuses."""
    with pytest.raises(ValueError):
        beads.retire(gated_parked.id, by="alex")

    cancelled = beads.cancel(gated_parked.id, by="alex", reason="moot now")
    assert cancelled.status is TaskStatus.CANCELLED


def test_cancel_works_on_ungated_work_too(beads, ungated):
    """Cancel is not gate-specific — abandoning plain work unfinished is the
    same claim ('we stopped caring, it never completed') regardless of gate."""
    cancelled = beads.cancel(ungated.id, by="alex", reason="deprioritized")
    assert cancelled.status is TaskStatus.CANCELLED


def test_the_executor_may_not_cancel_its_own_work(beads, gated_parked):
    """Authority mirrors approve_verify's separation (§9): self-cancelling
    unfinished work is the same self-grading the verify gate exists to stop."""
    with pytest.raises(ValueError, match="distinct route"):
        beads.cancel(gated_parked.id, by="executor-a", reason="I changed my mind")

    assert beads.get(gated_parked.id).status is TaskStatus.AWAITING_VERIFY


def test_cancel_refuses_without_a_reason(beads, gated_parked):
    with pytest.raises(ValueError, match="needs a reason"):
        beads.cancel(gated_parked.id, by="alex", reason="")


def test_cancel_refuses_without_a_named_canceller(beads, gated_parked):
    with pytest.raises(ValueError, match="who is cancelling it"):
        beads.cancel(gated_parked.id, by="", reason="moot")


def test_cancel_refuses_an_already_terminal_bead(beads, ungated):
    beads.cancel(ungated.id, by="alex", reason="first cancel")
    with pytest.raises(ValueError, match="already cancelled"):
        beads.cancel(ungated.id, by="alex", reason="second cancel")


def test_cancel_on_a_missing_task_returns_none(beads):
    assert beads.cancel("ac-doesnotexist", by="alex", reason="n/a") is None


def test_a_cancelled_bead_never_reenters_dispatch(beads, ungated):
    """CANCELLED must read as closed, the same as DONE/SKIPPED — ready() only
    ever offers PENDING work, so this is really asserting the enum addition
    didn't accidentally make cancel() land somewhere ready() still counts."""
    beads.cancel(ungated.id, by="alex", reason="deprioritized")
    assert ungated.id not in {t.id for t in beads.ready()}
