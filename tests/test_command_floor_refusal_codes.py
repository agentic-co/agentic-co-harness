"""ISC-4: the same rule produces the same refusal CODE under two executor adapters.

This tests the predeclared command floor rules (porting CommandFloorGuard.hook.ts)
to ensure they are backend-agnostic and use branchable machine codes.
"""

from __future__ import annotations

import pytest

import agentco_harness.orchestrator  # noqa: F401
from agentco_harness import command_floor
from agentco_harness.command_floor import (
    COMMAND_FLOOR_FORCE_PUSH_PROTECTED,
    COMMAND_FLOOR_CODES,
    CommandFloorViolation,
)
from agentco_harness.egress import AGENT_ROUTE

ADAPTER_A, ADAPTER_B = "claude", "agy"

def test_both_adapters_are_actually_routable_before_anything_is_claimed():
    assert AGENT_ROUTE.get(ADAPTER_A) == "NATIVE"
    assert AGENT_ROUTE.get(ADAPTER_B) == "BELLOWS", (
        "agy is not registered — importing agentco_harness.orchestrator is what "
        "registers it, and without it ISC-4's second adapter does not exist"
    )

def test_the_force_push_rule_yields_one_code_across_two_adapters():
    """ISC-4's actual bar, on the rule the module exists for."""
    command = "git push --force origin main"
    refusals = {}

    for adapter in (ADAPTER_A, ADAPTER_B):
        with pytest.raises(CommandFloorViolation) as caught:
            command_floor.check_command(command, adapter)
        refusals[adapter] = caught.value

    assert refusals[ADAPTER_A].code == refusals[ADAPTER_B].code == COMMAND_FLOOR_FORCE_PUSH_PROTECTED
    assert str(refusals[ADAPTER_A]) != str(refusals[ADAPTER_B]), (
        "the messages SHOULD differ — they name the agent explicitly."
    )

def test_implicit_push_without_current_branch_fails_closed_identically():
    command = "git push -f"
    refusals = {}

    for adapter in (ADAPTER_A, ADAPTER_B):
        with pytest.raises(CommandFloorViolation) as caught:
            command_floor.check_command(command, adapter)
        refusals[adapter] = caught.value

    assert refusals[ADAPTER_A].code == refusals[ADAPTER_B].code == COMMAND_FLOOR_FORCE_PUSH_PROTECTED
    assert str(refusals[ADAPTER_A]) != str(refusals[ADAPTER_B])

def test_command_floor_codes_are_not_asop_spec_codes():
    from asop import refusals

    assert COMMAND_FLOOR_CODES.isdisjoint(set(refusals.CODES)), (
        "a command floor code collides with an asop-spec refusal code"
    )
    assert all(c.startswith("command_floor:") for c in COMMAND_FLOOR_CODES)

def test_the_code_set_has_not_grown_silently():
    assert COMMAND_FLOOR_CODES == {
        "command_floor:force_push_protected",
    }

def test_force_with_lease_is_allowed():
    # Because it doesn't match bare --force
    command_floor.check_command("git push --force-with-lease origin main", ADAPTER_A)

def test_explicit_safe_branch_is_allowed():
    command_floor.check_command("git push -f origin feature-branch", ADAPTER_A)
    command_floor.check_command("git push --force myremote HEAD:feature-branch", ADAPTER_A)

def test_implicit_push_with_safe_current_branch_is_allowed():
    command_floor.check_command("git push -f", ADAPTER_A, current_branch="feature-branch")

def test_implicit_push_with_protected_current_branch_is_denied():
    with pytest.raises(CommandFloorViolation):
        command_floor.check_command("git push -f", ADAPTER_A, current_branch="main")

def test_tricky_protected_branch_references_are_caught():
    with pytest.raises(CommandFloorViolation):
        command_floor.check_command("git push -f origin HEAD:main", ADAPTER_A)

    with pytest.raises(CommandFloorViolation):
        command_floor.check_command("git push -f origin refs/heads/master", ADAPTER_A)
