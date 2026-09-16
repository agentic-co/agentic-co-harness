"""`gate_probe.py` — property-testing the verifier, and testing the property-tester.

N11: the PoC revised procedures by adversarially probing a gate's check
expression against constructed inputs, offline. This is the ASOP equivalent, and
these tests are the reason it is allowed to be believed.

THE TRUST GATE COMES FIRST. A probe that cannot rediscover a hole we already
know about is not evidence, it is decoration. Retail `asop.claude.v2.md` declares
15 deterministic gates and strands all 15 — a fact established independently by
`gate_reach.py`, which reaches it by PARSING. `gate_probe` must reach the same 15
by BEHAVIOUR, without being told. That is `test_the_probe_rediscovers_the_known_hole`.

AND THE PROBE MUST BE ABLE TO FAIL. The hub's conformance suite states the
principle plainly: "a harness that passes when a transport is broken has proven
only that it does not look." So one test hands the probe a check that accepts
absolutely everything and requires it to say so.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ASOPS = REPO / "evals" / "tau2-retail-asop" / "asops"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gate_probe = _load("gate_probe", REPO / "scripts" / "eval" / "gate_probe.py")

#: tau2-retail's actual tool vocabulary. The sibling probe is meaningless
#: without it — see `_siblings_accepted`'s docstring for what fabricating names
#: instead did to an earlier draft.
RETAIL_TOOLS = (
    "calculate",
    "cancel_pending_order",
    "exchange_delivered_order_items",
    "find_user_id_by_email",
    "find_user_id_by_name_zip",
    "get_order_details",
    "get_product_details",
    "get_user_details",
    "list_all_product_types",
    "modify_pending_order_address",
    "modify_pending_order_items",
    "modify_pending_order_payment",
    "modify_user_address",
    "return_delivered_order_items",
    "think",
    "transfer_to_human_agents",
)


def _verdicts(doc: str, vocabulary=RETAIL_TOOLS):
    results = gate_probe.probe_document(ASOPS / doc, vocabulary)
    counts: dict[str, int] = {}
    for r in results:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    return results, counts


# ------------------------------------------------------------- the trust gate


def test_the_probe_rediscovers_the_known_hole():
    """v2 strands all 15 of its declared deterministic gates.

    `gate_reach.py` found this by reading the document. If `gate_probe` cannot
    find the same 15 by running the check, the two disagree and at least one is
    wrong — which is the point of having two derivations rather than one.
    """
    results, counts = _verdicts("asop.claude.v2.md")
    assert len(results) == 15, "v2 declares 15 deterministic gates"
    assert counts.get("CHECKS NOTHING") == 15, counts
    assert counts.get("DISCRIMINATES", 0) == 0, counts


def test_the_probe_can_fail_on_a_check_that_accepts_everything(monkeypatch):
    """The probe must catch the PoC's bug class, stated in its purest form.

    `ship-a-fix`'s gate was satisfied by the string "everything passed fine".
    Here the check is worse — it accepts literally any evidence — and the probe
    has to name it rather than report a clean document. A probe that passes this
    test has proven only that it does not look.
    """
    monkeypatch.setattr(
        gate_probe.asop_agent, "check_tool_succeeded",
        lambda tool, history: (True, "sure, looks fine"),
    )
    _results, counts = _verdicts("asop.claude.v3.md")
    assert counts.get("FALSE ACCEPT") == 15, counts
    assert counts.get("DISCRIMINATES", 0) == 0, counts


def test_the_probe_can_fail_on_a_check_that_refuses_everything(monkeypatch):
    """The other broken direction, which is loud rather than silent.

    Named separately because the verdicts must not be collapsed: a check that
    refuses everything fails every run and somebody notices within the hour, so
    it is a different severity from one that accepts everything and never does.
    """
    monkeypatch.setattr(
        gate_probe.asop_agent, "check_tool_succeeded",
        lambda tool, history: (False, "no"),
    )
    _results, counts = _verdicts("asop.claude.v3.md")
    assert counts.get("REFUSES EVERYTHING") == 15, counts


# ------------------------------------------------------- what v3 actually is


def test_v3_declares_nothing_that_checks_nothing():
    """The fix stage 6 shipped, asserted from behaviour rather than from a run."""
    _results, counts = _verdicts("asop.claude.v3.md")
    assert counts.get("CHECKS NOTHING", 0) == 0, counts
    assert counts.get("FALSE ACCEPT", 0) == 0, counts
    assert counts.get("DISCRIMINATES") == 10, counts


def test_the_find_user_id_prefix_accepts_exactly_the_two_documented_siblings():
    """Pins retail's one deliberate prefix match, so a third collision surfaces.

    `asop.claude.v3.NOTES.md` reasons this through and declares it intentional:
    authentication succeeds via `find_user_id_by_email` OR
    `find_user_id_by_name_zip`, and naming either one would refuse every
    conversation that used the other. That is an AFFORDANCE.

    It stops being one the moment a third tool starts matching the same prefix,
    because nothing would announce that. This test is the announcement.
    """
    results, _counts = _verdicts("asop.claude.v3.md")
    siblings = {r.tool: r.siblings for r in results if r.verdict == "ACCEPTS SIBLINGS"}
    assert set(siblings) == {"find_user_id"}, (
        f"a gate other than the documented `find_user_id` prefix now accepts a "
        f"sibling tool: {sorted(set(siblings) - {'find_user_id'})}. Decide whether "
        f"it is a defect or an affordance and record it in the document's .NOTES.md."
    )
    assert siblings["find_user_id"] == (
        "find_user_id_by_email", "find_user_id_by_name_zip",
    ), siblings["find_user_id"]


def test_the_sibling_probe_is_silent_without_a_vocabulary():
    """No `--tools`, no sibling findings — and the CLI says so out loud.

    Reporting "0 accepting siblings" when the probe never ran would be a clean
    bill of health issued without looking, which is the failure mode this whole
    script is named after.
    """
    results = gate_probe.probe_document(ASOPS / "asop.claude.v3.md", ())
    assert not any(r.verdict == "ACCEPTS SIBLINGS" for r in results)
    assert not any(r.siblings for r in results)


# ------------------------------------------------------------ the probe table


@pytest.mark.parametrize("probe", gate_probe.PROBES, ids=lambda p: p.name)
def test_every_probe_says_why_it_matters(probe):
    """A probe whose failure message does not explain itself gets suppressed.

    This is cheap, and it is the difference between a finding somebody acts on
    and a finding somebody adds to an ignore list.
    """
    assert probe.why.strip(), f"probe {probe.name!r} has no rationale"
    assert len(probe.why) > 30, f"probe {probe.name!r} explains too little"


def test_the_table_probes_more_failures_than_successes():
    """Anybody can write a check that passes good evidence.

    The question a gate exists to answer is whether it REFUSES bad evidence, so
    a probe table weighted toward the happy path is measuring the wrong thing.
    """
    refusing = [p for p in gate_probe.PROBES if not p.expect]
    passing = [p for p in gate_probe.PROBES if p.expect]
    assert len(passing) >= 1, "something must establish the gate can pass at all"
    assert len(refusing) > len(passing), (
        f"{len(refusing)} refusal probes vs {len(passing)} pass probes — "
        "a table that mostly checks the happy path proves the happy path"
    )
