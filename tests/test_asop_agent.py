"""Tests for the arm (c) adapter.

The properties worth testing here are not "does it run". They are the three
things that would let arm (c) post a number it had not earned:

  * a gate that can see the gold state,
  * an executor clearing its own gate,
  * a verifier whose non-answer is read as a pass.

Each of those fails silently in a run that exits cleanly, so each gets a test
that proves the refusal happens rather than that the happy path works.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Loaded by path: scripts/eval is a tool directory, not a package, and the
# runtime must not grow an import of a benchmark adapter. Registering in
# sys.modules BEFORE exec is required — @dataclass resolves a class's
# __module__ through sys.modules and blows up on None otherwise.
_SRC = Path(__file__).resolve().parents[1] / "scripts" / "eval" / "asop_agent.py"
_spec = importlib.util.spec_from_file_location("asop_agent", _SRC)
assert _spec and _spec.loader
aa = importlib.util.module_from_spec(_spec)
sys.modules["asop_agent"] = aa
_spec.loader.exec_module(aa)

ASOPS = Path(__file__).resolve().parents[1] / "evals" / "tau2-airline-asop" / "asops"


# ── parsing the real documents ───────────────────────────────────────────────


@pytest.mark.parametrize("name", ["asop.claude.md", "asop.codex.md", "asop.agy.md"])
def test_parses_every_extracted_asop(name):
    """All three arms' documents must parse.

    Three models wrote these independently and none agreed on layout. A parser
    that only handled one shape would yield zero steps for the others — and a
    zero-step procedure makes an arm look gate-free rather than unparsed, which
    is the kind of difference that quietly becomes a result.
    """
    path = ASOPS / name
    if not path.exists():
        pytest.skip(f"{name} not in the tree")
    asop = aa.parse_asop(path.read_text())
    assert asop.procedures, f"{name}: no procedures"
    for proc in asop.procedures:
        assert proc.steps, f"{name}: {proc.name} parsed to zero steps"
        for step in proc.steps:
            assert step.gate_kinds, f"{name}: {step.label} has no gate"


def test_parse_refuses_a_document_with_no_procedures():
    with pytest.raises(ValueError, match="no .## . sections|yielded a step"):
        aa.parse_asop("# Just a title\n\nSome prose.\n")


SAMPLE = """\
# T

Rules here.

## Procedure: Cancel Flight

1. **Obtain user id.** Gate: human.
2. **Check eligibility.** Precondition: must hold. Gate: deterministic (agent-side rule check).
3. **Execute.** Gate: human, then deterministic (tool call).
"""


def test_gate_kinds_are_read_off_the_step():
    asop = aa.parse_asop(SAMPLE)
    steps = asop.procedure("Cancel Flight").steps
    assert steps[0].gate_kinds == (aa.GateKind.HUMAN,)
    assert steps[2].gate_kinds == (aa.GateKind.HUMAN, aa.GateKind.DETERMINISTIC)


def test_a_rule_check_is_not_a_deterministic_gate():
    """"deterministic (agent-side rule check)" names a rule, not a command.

    Recording it as DETERMINISTIC would let the arm claim a re-runnable check
    it does not have. It degrades to a judged verdict, and says so.
    """
    asop = aa.parse_asop(SAMPLE)
    step = asop.procedure("Cancel Flight").steps[1]
    assert step.gate_kinds == (aa.GateKind.DETERMINISTIC_UNAVAILABLE,)


# ── the three silent failures ────────────────────────────────────────────────


def test_executor_cannot_attest_its_own_work():
    with pytest.raises(ValueError, match="attested its own work|separation"):
        aa.Verdict(
            passed=True,
            reason="looks fine",
            gate_kind=aa.GateKind.JUDGED,
            verifier="agent-1",
            executor="agent-1",
        )


def test_verifier_refuses_to_judge_when_it_is_the_executor():
    v = aa.Verifier("agent-1", judge=lambda _p: (True, "fine"))
    step = aa.parse_asop(SAMPLE).procedure("Cancel Flight").steps[0]
    ev = aa.Evidence(step=step, transcript=(), tool_calls=())
    with pytest.raises(ValueError, match="separation"):
        v.attest(ev, aa.GateKind.JUDGED, executor="agent-1")


def test_a_verdict_must_say_what_it_found():
    """An identity and a timestamp are not a verdict; neither is a bare PASS."""
    with pytest.raises(ValueError, match="what it found"):
        aa.Verdict(
            passed=True,
            reason="   ",
            gate_kind=aa.GateKind.JUDGED,
            verifier="v",
            executor="e",
        )


@pytest.mark.parametrize(
    "raw",
    ["", "I think it probably passed", "{}", "PASSED maybe", "Sure!"],
)
def test_an_unparseable_verdict_is_a_failure_not_a_pass(raw):
    """A judge that did not answer has not passed the gate.

    Defaulting the other way is how a broken verifier turns a whole arm green.
    """
    passed, reason = aa.parse_verdict(raw)
    assert passed is False
    assert reason


def test_parse_verdict_reads_both_directions():
    assert aa.parse_verdict("PASS — user id was supplied at turn 2")[0] is True
    assert aa.parse_verdict("FAIL — no cabin class was confirmed")[0] is False


# ── the leakage rule ─────────────────────────────────────────────────────────


class _TaskShaped:
    """Anything carrying the answer key."""

    evaluation_criteria = object()


class _EnvShaped:
    def get_db_hash(self):  # pragma: no cover - never called
        return "deadbeef"


@pytest.mark.parametrize("obj", [_TaskShaped(), _EnvShaped()])
def test_gate_evaluator_refuses_anything_carrying_gold(obj):
    """The failure this prevents does not announce itself.

    A gate with reach into the gold state passes exactly the runs it should and
    the arm posts a beautiful, meaningless number.
    """
    with pytest.raises(ValueError, match="gold state|exposes"):
        aa._assert_no_gold(obj, "the gate evaluator")


def test_evidence_carries_nothing_task_shaped():
    step = aa.parse_asop(SAMPLE).procedure("Cancel Flight").steps[0]
    ev = aa.Evidence(step=step, transcript=("user: hi",), tool_calls=())
    aa._assert_no_gold(ev, "the gate evaluator")  # must not raise
    for attr in aa._GOLD_ATTRS:
        assert not hasattr(ev, attr)


# ── the agent, when tau2 is present ──────────────────────────────────────────

_HAS_TAU2 = importlib.util.find_spec("tau2") is not None
needs_tau2 = pytest.mark.skipif(_HAS_TAU2 is False, reason="tau2 checkout not on path")


@needs_tau2
def test_agent_refuses_to_run_without_a_verifier():
    """Arm (c) without a verifier is arm (b) wearing its name."""
    with pytest.raises(ValueError, match="requires a verifier"):
        aa.ASOPAgent(tools=[], domain_policy=SAMPLE, llm="x", verifier=None)


# ── routing, added in ASOP v2 ────────────────────────────────────────────────


def test_v1_has_no_routing_and_v2_does():
    """The first real arm (c) run localised a failure to a missing entry point.

    v1 of every extraction jumps straight into procedures without saying how to
    pick one, so "I'd like to change my flight" matched nothing and the executor
    jammed on step 1 of whichever procedure a stray word hit. v2 adds the table
    that was missing. This test is the regression guard on that revision.
    """
    v1 = aa.parse_asop((ASOPS / "asop.claude.md").read_text())
    v2 = aa.parse_asop((ASOPS / "asop.claude.v2.md").read_text())
    assert v1.routing == ()
    assert len(v2.routing) > 10


def test_v2_routes_the_request_that_v1_misrouted():
    v2 = aa.parse_asop((ASOPS / "asop.claude.v2.md").read_text())
    said = "i'd like to change my flight"
    hit = next(
        (t for p, t in sorted(v2.routing, key=lambda pr: -len(pr[0])) if p in said),
        None,
    )
    assert hit == "Modify Flight"


def test_longer_routing_phrases_win():
    """"change cabin" must not lose to a bare "change" that happens to sort first."""
    v2 = aa.parse_asop((ASOPS / "asop.claude.v2.md").read_text())
    ordered = [p for p, _ in sorted(v2.routing, key=lambda pr: -len(pr[0]))]
    assert len(ordered[0]) >= len(ordered[-1])
