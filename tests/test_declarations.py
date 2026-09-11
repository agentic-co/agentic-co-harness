"""The declared registries, and what they refuse.

Finding 17: this runtime read `$USER` and compared strings. The spec calls that
out by name — three implementations shipped "differs from the executor" as the
whole check and called it verification, and §5.3 says plainly it "is a mistake
detector, not a credential."

The tests worth having are the ones that fail if the rail is decorative: that
an undeclared registry refuses rather than waves through, that the refusal is
distinguishable from a malformed call, that the pre-split variable name still
works, and that an operator's deliberately EMPTY declaration is not silently
overridden by a stale one.
"""

from __future__ import annotations

import pytest

from agentco_harness import declarations
from agentco_harness.beads import Beads, TaskStatus

GATE = {"class": "judged", "check": "is the migration reversible?"}


@pytest.fixture
def parked(tmp_path):
    """A bead sitting at a judged gate, the honest way."""
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create("migrate", "d", metadata={"verify": dict(GATE)})
    claimed = beads.claim(task.id, "executor-a")
    beads.report_result(task.id, claimed.lease_attempt, TaskStatus.DONE, result="done")
    assert beads.get(task.id).status is TaskStatus.AWAITING_VERIFY
    return beads, task


def test_an_undeclared_registry_authenticates_nobody(parked, monkeypatch):
    """The whole point, and the opposite of what this runtime did before.

    ASOP §9: "A registry with nothing declared authenticates nobody; there is
    no fallback that authenticates everybody." Failing open here is the shape
    that makes every other rail decorative — the gate parks correctly, the
    verdict is recorded correctly, and anyone at all releases it.
    """
    beads, task = parked
    monkeypatch.delenv("ASOP_VERIFIERS", raising=False)
    monkeypatch.delenv("AGENTCO_VERIFIERS", raising=False)

    with pytest.raises(declarations.Unauthenticated, match="no registry is declared"):
        beads.approve_verify(task.id, approver="anybody", reason="looks fine")

    assert beads.get(task.id).status is TaskStatus.AWAITING_VERIFY


def test_an_actor_outside_the_registry_is_refused(parked, monkeypatch):
    beads, task = parked
    monkeypatch.setenv("ASOP_VERIFIERS", "dana,sam")

    with pytest.raises(declarations.Unauthenticated, match="not in the declared"):
        beads.approve_verify(task.id, approver="stranger", reason="looks fine")

    assert beads.get(task.id).status is TaskStatus.AWAITING_VERIFY


def test_a_declared_actor_may_answer_the_gate(parked, monkeypatch):
    """The rail must not become a wall — the positive case has to work, or the
    only safe configuration is one where no gate is ever answered."""
    beads, task = parked
    monkeypatch.setenv("ASOP_VERIFIERS", "dana,sam")

    approved = beads.approve_verify(task.id, approver="dana", reason="reversal tested")

    assert approved.status is TaskStatus.DONE
    assert approved.metadata["verify_approval"]["verdict"]["reason"] == "reversal tested"


def test_rejecting_is_authenticated_on_the_same_terms(parked, monkeypatch):
    """A rejection is an attestation with a false verdict. Leaving this side
    open means anyone can fail a gate they were never permitted to answer."""
    beads, task = parked
    monkeypatch.setenv("ASOP_VERIFIERS", "dana")

    with pytest.raises(declarations.Unauthenticated):
        beads.reject_verify(task.id, approver="stranger", reason="no")

    assert beads.get(task.id).status is TaskStatus.AWAITING_VERIFY
    rejected = beads.reject_verify(task.id, approver="dana", reason="not reversible")
    assert rejected.status is TaskStatus.VERIFY_FAILED


def test_authentication_runs_before_the_distinctness_check(parked, monkeypatch):
    """Order matters, and the wrong order is the bug the spec describes.

    If "differs from the executor" ran first, an unauthenticated stranger would
    pass whenever they happened to pick a name other than the executor's — the
    mistake detector standing in for the credential, exactly as §5.3 warns.
    """
    beads, task = parked
    monkeypatch.delenv("ASOP_VERIFIERS", raising=False)
    monkeypatch.delenv("AGENTCO_VERIFIERS", raising=False)

    # 'executor-a' held the lease, so this name is distinct from the executor
    # and would sail through a distinctness-only check.
    with pytest.raises(declarations.Unauthenticated):
        beads.approve_verify(task.id, approver="not-the-executor", reason="fine")


def test_the_pre_split_variable_still_works(monkeypatch):
    """A deployment that predates the ASOP_* rename keeps running — the same
    courtesy the plane extends, and the reason finding 17 was survivable."""
    monkeypatch.delenv("ASOP_VERIFIERS", raising=False)
    monkeypatch.setenv("AGENTCO_VERIFIERS", "dana")
    assert declarations.verifiers() == frozenset({"dana"})


def test_the_standard_name_wins_over_the_legacy_one(monkeypatch):
    monkeypatch.setenv("ASOP_VERIFIERS", "sam")
    monkeypatch.setenv("AGENTCO_VERIFIERS", "dana")
    assert declarations.verifiers() == frozenset({"sam"})


def test_a_deliberately_empty_declaration_is_not_overridden(monkeypatch):
    """Presence, not truthiness.

    An operator who sets the standard's variable to empty means "nobody" by it.
    Falling through to a stale legacy value would silently re-authorise the
    people they just removed — the failure being silent is what makes it worth
    a test.
    """
    monkeypatch.setenv("ASOP_VERIFIERS", "")
    monkeypatch.setenv("AGENTCO_VERIFIERS", "dana")
    assert declarations.verifiers() == frozenset()


def test_an_unnamed_actor_resolves_against_nothing(monkeypatch):
    monkeypatch.setenv("ASOP_VERIFIERS", "dana")
    with pytest.raises(declarations.Unauthenticated, match="no verifier named"):
        declarations.authenticate("", declarations.verifiers(), role="verifier")


def test_the_refusal_carries_the_contract_code():
    """§10 gives this its own refusal code, and a caller cannot distinguish
    'not allowed' from 'malformed' by reading a message."""
    assert declarations.Unauthenticated.code == "unauthenticated"
    assert issubclass(declarations.Unauthenticated, ValueError)


def test_a_forged_approval_in_the_payload_does_not_release_the_gate(parked, monkeypatch):
    """The bypass flag is gone; this is what replaced it, and why.

    `verify_gate=False` used to be the sanctioned way to DONE for an answered
    gate — a caller asserting somebody vouched. Removing it moves the evidence
    into the record, and the record travels in metadata, which arrives from the
    caller. So "there is an approval on it" cannot be the test, or the bypass
    is back with a longer name: authority read off an incoming payload, which
    is finding 1's shape.

    Every condition is re-checked at the choke point instead. Here the forged
    approval is well-formed and names an undeclared approver.
    """
    beads, task = parked
    monkeypatch.setenv("ASOP_VERIFIERS", "dana")

    forged = dict(beads.get(task.id).metadata)
    forged["verify_approval"] = {
        "approver": "stranger",
        "approved_at": "2026-09-10T00:00:00+00:00",
        "verdict": {"passed": True, "reason": "trust me"},
    }
    beads.update(task.id, status=TaskStatus.DONE, metadata=forged)

    assert beads.get(task.id).status is TaskStatus.AWAITING_VERIFY


def test_a_forged_approval_naming_the_executor_does_not_release_it_either(
    parked, monkeypatch
):
    """Authenticated is not sufficient — the separation check runs here too.

    Otherwise a declared verifier who was also the executor could release their
    own work by writing the record directly, skipping the one refusal
    `approve_verify` exists to make.
    """
    beads, task = parked
    monkeypatch.setenv("ASOP_VERIFIERS", "executor-a")

    forged = dict(beads.get(task.id).metadata)
    forged["verify_approval"] = {
        "approver": "executor-a",
        "approved_at": "2026-09-10T00:00:00+00:00",
        "verdict": {"passed": True, "reason": "mine is fine"},
    }
    beads.update(task.id, status=TaskStatus.DONE, metadata=forged)

    assert beads.get(task.id).status is TaskStatus.AWAITING_VERIFY


def test_a_verdictless_approval_does_not_release_it(parked, monkeypatch):
    """A name and a timestamp show a party was named, not that a party looked."""
    beads, task = parked
    monkeypatch.setenv("ASOP_VERIFIERS", "dana")

    forged = dict(beads.get(task.id).metadata)
    forged["verify_approval"] = {
        "approver": "dana",
        "approved_at": "2026-09-10T00:00:00+00:00",
        "verdict": {"passed": True, "reason": "   "},
    }
    beads.update(task.id, status=TaskStatus.DONE, metadata=forged)

    assert beads.get(task.id).status is TaskStatus.AWAITING_VERIFY


def test_a_real_approval_still_releases_the_gate(parked, monkeypatch):
    """The rail must not become a wall — and this is the path `approve_verify`
    now takes, through the same check as everyone else."""
    beads, task = parked
    monkeypatch.setenv("ASOP_VERIFIERS", "dana")

    approved = beads.approve_verify(task.id, approver="dana", reason="reversal tested")

    assert approved.status is TaskStatus.DONE
    assert approved.metadata["verify_result"]["passed"] is True
