"""ISC-4: the same rule produces the same refusal CODE under two executor adapters.

Goal G1 (principal, 2026-09-16: *"hooks should be high priority"*). ISC-4 asks
that every enforcement rule which today lives as a Claude Code hook be
re-expressed as backend-agnostic policy, **verified by the same rule producing
the same refusal under at least two different executor adapters.**

`egress.py` is the worked example: `EgressClassGuard.hook.ts` is a Claude Code
PreToolUse gate that only fires inside an interactive session, while the
unattended path dispatched to a PUBLIC-ceiling vendor with no classification
awareness at all. The module already closed that gap. What it did NOT have was a
**code** — every refusal was prose, and every message interpolates the agent and
the route name, so two adapters refused by one rule produced two different
strings *by construction*. ISC-4 cannot be satisfied by a string that is
different every time on purpose.

The hub carries the identical defect on its own open list — `MCP refusals`,
where a machine code "survives only as a string prefix" while `errors.py` says
clients branch on the code. Same mistake, two repositories, which is the
argument for fixing the shape rather than the instance.

WHY THE CODES ARE NOT `asop-spec` CODES. `asop.refusals.CODES` is the protocol's
vocabulary, and this repo's invariant is that the spec "arrives by version, never
by path". Egress classification is runtime policy — which vendor a bead may be
dispatched to — and the plane has no opinion on it. So these live in their own
`egress:` namespace, and one test below asserts the two namespaces stay disjoint.
"""

from __future__ import annotations

import pytest

# Importing the orchestrator is what REGISTERS the executor backends, and
# registration is what populates `AGENT_ROUTE`. Without it only the four
# bootstrap entries exist and `agy` is unroutable — which would make this file
# pass for the wrong reason.
import agentco_harness.orchestrator  # noqa: F401
from agentco_harness import egress
from agentco_harness.egress import (
    EGRESS_CEILING_EXCEEDED,
    EGRESS_CODES,
    EGRESS_ROUTE_ABSENT,
    EGRESS_UNKNOWN_DATA_CLASS,
    AGENT_ROUTE,
    EgressDenied,
    Route,
)

#: The two adapters ISC-4 names. `claude` is the native Anthropic route; `agy`
#: is Google's Antigravity CLI on the BELLOWS route — a genuinely different
#: vendor, which is the point. A second adapter on the same vendor would prove
#: nothing about backend-agnosticism.
ADAPTER_A, ADAPTER_B = "claude", "agy"


def _table(ceiling: str) -> dict[str, Route]:
    """A policy artifact where every registered route carries `ceiling`.

    Built from `AGENT_ROUTE` rather than hand-listed so a newly registered
    backend is covered automatically instead of silently skipping the test.
    """
    return {
        route: Route(
            name=route,
            vendor=f"vendor-for-{route.lower()}",
            model="m",
            ceiling=ceiling,  # type: ignore[arg-type]
            ceiling_unsupervised=ceiling,  # type: ignore[arg-type]
            ceiling_verified=True,
        )
        for route in set(AGENT_ROUTE.values()) - set(egress.NON_EGRESS_ROUTES)
    }


def test_both_adapters_are_actually_routable_before_anything_is_claimed():
    """Guards the way this file could pass while testing nothing.

    If `agy` had no route, every assertion below would still hold — both
    adapters would refuse with `route_undeclared` and the codes would match,
    proving only that two unroutable agents are equally unroutable.
    """
    assert AGENT_ROUTE.get(ADAPTER_A) == "NATIVE"
    assert AGENT_ROUTE.get(ADAPTER_B) == "BELLOWS", (
        "agy is not registered — importing agentco_harness.orchestrator is what "
        "registers it, and without it ISC-4's second adapter does not exist"
    )


# ------------------------------------------------------------------ ISC-4 core


def test_the_ceiling_rule_yields_one_code_across_two_adapters():
    """ISC-4's actual bar, on the rule the module exists for.

    A CONFIDENTIAL bead against PUBLIC-ceiling routes. Both adapters refuse; the
    assertion is that they refuse with the SAME CODE and DIFFERENT messages —
    the code is the branchable fact, the message is the human one.
    """
    table = _table("PUBLIC")
    metadata = {"data_class": "CONFIDENTIAL"}

    refusals = {}
    for adapter in (ADAPTER_A, ADAPTER_B):
        with pytest.raises(EgressDenied) as caught:
            egress.check_egress(adapter, metadata, routes=table, supervised=False)
        refusals[adapter] = caught.value

    assert refusals[ADAPTER_A].code == refusals[ADAPTER_B].code == EGRESS_CEILING_EXCEEDED
    assert str(refusals[ADAPTER_A]) != str(refusals[ADAPTER_B]), (
        "the messages SHOULD differ — they name the route and vendor. That is "
        "exactly why the code has to exist separately from the prose."
    )


def test_the_missing_route_rule_yields_one_code_across_two_adapters():
    """A second rule, because one rule agreeing could be a coincidence."""
    empty: dict[str, Route] = {}

    codes = set()
    for adapter in (ADAPTER_A, ADAPTER_B):
        with pytest.raises(EgressDenied) as caught:
            egress.check_egress(adapter, {}, routes=empty, supervised=False)
        codes.add(caught.value.code)

    assert codes == {EGRESS_ROUTE_ABSENT}


def test_different_rules_do_not_share_a_code():
    """A code that never varies is as useless as no code at all.

    If every refusal carried `egress:denied`, the tests above would pass and a
    caller still could not branch. This is the falsifier for that.
    """
    table = _table("PUBLIC")
    with pytest.raises(EgressDenied) as ceiling:
        egress.check_egress(ADAPTER_A, {"data_class": "RESTRICTED"}, routes=table)
    with pytest.raises(EgressDenied) as unknown:
        egress.check_egress(ADAPTER_A, {"data_class": "NONSENSE"}, routes=table)

    assert ceiling.value.code == EGRESS_CEILING_EXCEEDED
    assert unknown.value.code == EGRESS_UNKNOWN_DATA_CLASS
    assert ceiling.value.code != unknown.value.code


# ------------------------------------------------------------ namespace hygiene


def test_egress_codes_are_not_asop_spec_codes():
    """The spec arrives by version, never by path — so do not fork its vocabulary.

    `asop.refusals.CODES` is the protocol's. These are runtime policy's. Keeping
    them provably disjoint is what stops a reader treating an `egress:` code as a
    contract guarantee the spec never made.
    """
    from asop import refusals

    assert EGRESS_CODES.isdisjoint(set(refusals.CODES)), (
        "an egress code collides with an asop-spec refusal code — rename the "
        "local one; the spec's vocabulary is not ours to extend from here"
    )
    assert all(c.startswith("egress:") for c in EGRESS_CODES)


def test_every_raised_code_is_a_declared_one():
    """No refusal escapes carrying the unclassified fallback.

    `_CodedRefusal.default_code` exists so a code is never None, but an
    `egress:unclassified` reaching a caller means a raise site was added without
    being classified — which is the prose problem coming back wearing a code.
    """
    table = _table("PUBLIC")
    cases = [
        (ADAPTER_A, {"data_class": "CONFIDENTIAL"}, table),
        (ADAPTER_B, {"data_class": "CONFIDENTIAL"}, table),
        (ADAPTER_A, {"data_class": "NONSENSE"}, table),
        (ADAPTER_A, {}, {}),
        (ADAPTER_B, {}, {}),
    ]
    for adapter, metadata, routes in cases:
        with pytest.raises(EgressDenied) as caught:
            egress.check_egress(adapter, metadata, routes=routes, supervised=False)
        assert caught.value.code in EGRESS_CODES, (
            f"{adapter} raised an undeclared code {caught.value.code!r}"
        )


def test_the_code_set_has_not_grown_silently():
    """A new rule must be declared, the same way a new terminal door must be.

    Same mechanism as `test_terminal_paths.py`: the point is not to freeze the
    number, it is to make a new one announce itself.
    """
    assert EGRESS_CODES == {
        "egress:unknown_data_class",
        "egress:route_undeclared",
        "egress:route_absent",
        "egress:ceiling_exceeded",
        "egress:policy_unavailable",
    }
