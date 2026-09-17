"""`gate_value.py` — does a gate's refusal predict anything, on real runs?

Goal G3. N15 established the refusal RATE (~50% per gate, compounding to a
composite that refused 31 of 31 correct completions). Rate alone does not
condemn a gate: refusing half of everything is fine if it refuses the bad half.

So this measures **lift** — `P(refuse | run wrong) − P(refuse | run correct)`:

    > +0.05  informative
    ≈  0     noise; the false refusals buy nothing
    < −0.05  misleading; refuses correct runs MORE than failing ones

`gate_probe.py` asks whether a gate *can* discriminate against constructed
evidence. This asks whether it *does*, against evidence real runs produced. A
gate can pass the first and fail the second, and that gap is the finding.

WHAT THESE TESTS PROTECT. Two things, both of which this project has got wrong
before with numbers:

1. **The n-floor.** A lift computed over three conversations looks exactly like
   one computed over three hundred. `MIN_PER_CLASS` makes the thin ones
   unreportable rather than quietly confident, and a test pins that it is
   enforced on BOTH outcome classes — a gate seen in forty passing runs and two
   failing ones is not measurable, however large the first number looks.
2. **Undefined ≠ zero.** A gate with no failing conversations has no lift, not a
   lift of 0.0. Reporting 0.0 there would file it under "noise" — condemning a
   gate for evidence nobody collected.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location("gate_value", REPO / "scripts" / "eval" / "gate_value.py")
gate_value = importlib.util.module_from_spec(spec)
sys.modules["gate_value"] = gate_value
spec.loader.exec_module(gate_value)


def _verdict(conv: int, passed: bool, step: int = 1, verifier: str = "deterministic-check") -> dict:
    return {
        "conversation": conv, "passed": passed, "step": step,
        "procedure": "P", "step_title": "s", "verifier": verifier,
        "not_applicable": False,
    }


def _rows(correct_n: int, correct_refused: int, wrong_n: int, wrong_refused: int) -> dict:
    """Build a reward map + verdicts with exactly the requested refusal counts."""
    reward, verdicts, conv = {}, [], 0
    for i in range(correct_n):
        reward[conv] = True
        verdicts.append(_verdict(conv, passed=(i >= correct_refused)))
        conv += 1
    for i in range(wrong_n):
        reward[conv] = False
        verdicts.append(_verdict(conv, passed=(i >= wrong_refused)))
        conv += 1
    return gate_value.per_gate(reward, verdicts)


def test_lift_is_the_difference_between_the_two_refusal_rates():
    rows = _rows(correct_n=20, correct_refused=4, wrong_n=20, wrong_refused=16)
    r = next(iter(rows.values()))
    pc = r["correct_refused"] / r["correct_n"]
    pw = r["wrong_refused"] / r["wrong_n"]
    assert pc == 0.20 and pw == 0.80
    assert round(pw - pc, 2) == 0.60, "an informative gate: refuses failures far more often"


def test_a_gate_that_refuses_both_classes_equally_is_noise():
    """The finding this script exists to surface.

    Half of everything refused, and the half is not the failing half. Every one
    of those refusals on a correct run is pure cost.
    """
    rows = _rows(correct_n=20, correct_refused=10, wrong_n=20, wrong_refused=10)
    r = next(iter(rows.values()))
    lift = r["wrong_refused"] / r["wrong_n"] - r["correct_refused"] / r["correct_n"]
    assert lift == 0.0


def test_a_gate_can_be_worse_than_noise():
    """Negative lift: it refuses correct runs MORE than failing ones.

    Measured in the archive, not hypothetical — three gates across the gated
    runs come out negative.
    """
    rows = _rows(correct_n=20, correct_refused=18, wrong_n=20, wrong_refused=8)
    r = next(iter(rows.values()))
    lift = r["wrong_refused"] / r["wrong_n"] - r["correct_refused"] / r["correct_n"]
    assert lift < -0.05


@pytest.mark.parametrize("correct_n,wrong_n", [(40, 2), (2, 40), (9, 9), (0, 20), (20, 0)])
def test_thin_evidence_is_unreportable_on_either_side(correct_n, wrong_n, capsys):
    """A big number on one side does not license a lift.

    A gate seen in forty passing runs and two failing ones has no measurable
    association, and the forty is exactly what makes it look like it does.
    """
    reward, verdicts, conv = {}, [], 0
    for _ in range(correct_n):
        reward[conv] = True; verdicts.append(_verdict(conv, passed=False)); conv += 1
    for _ in range(wrong_n):
        reward[conv] = False; verdicts.append(_verdict(conv, passed=False)); conv += 1

    rows = gate_value.per_gate(reward, verdicts)
    r = next(iter(rows.values()))
    reportable = r["correct_n"] >= gate_value.MIN_PER_CLASS and r["wrong_n"] >= gate_value.MIN_PER_CLASS
    assert not reportable, (
        f"correct={correct_n} wrong={wrong_n} was treated as reportable; the floor "
        f"must apply to BOTH classes, not to their sum"
    )


def test_not_applicable_verdicts_are_excluded():
    """A conditional step that never triggered is not a pass and not a refusal.

    Counting it either way would move every rate on every gate that has one.
    """
    reward = {0: True, 1: False}
    verdicts = [
        _verdict(0, passed=True),
        {**_verdict(1, passed=False), "not_applicable": True},
    ]
    applicable = [v for v in verdicts if not v.get("not_applicable")]
    rows = gate_value.per_gate(reward, applicable)
    r = next(iter(rows.values()))
    assert r["correct_n"] == 1 and r["wrong_n"] == 0


def test_deterministic_and_judged_are_labelled_not_pooled():
    """phase-2.md: a liveness result must never be reported as a correctness one.

    The kind travels with the row so the two can never be silently averaged.
    """
    reward = {0: True, 1: False}
    verdicts = [
        _verdict(0, passed=False, verifier="deterministic-check"),
        _verdict(1, passed=False, verifier="verifier:some-model"),
    ]
    rows = gate_value.per_gate(reward, verdicts)
    kinds = next(iter(rows.values()))["kind"]
    assert kinds == {"det", "judged"}


def test_the_archive_still_reproduces_the_finding():
    """The measured result on real data, pinned so a later change has to explain it.

    Across the gated runs in the archive, almost no gate's refusal predicts
    whether the run succeeded. If this starts failing, either the gates or the
    scoring did — and both are worth stopping for.
    """
    archive = Path.home() / "Code/agentco-harness-eval-archive/2026-09-15/retail_v3_fixed"
    if not (archive / "verdicts.jsonl").exists():
        pytest.skip("eval archive not present on this machine")

    reward, verdicts = gate_value.load(archive)
    rows = gate_value.per_gate(reward, verdicts)
    reportable = [
        r for r in rows.values()
        if r["correct_n"] >= gate_value.MIN_PER_CLASS and r["wrong_n"] >= gate_value.MIN_PER_CLASS
    ]
    assert reportable, "no gate cleared the n-floor — the pin has nothing to hold"
    lifts = [r["wrong_refused"] / r["wrong_n"] - r["correct_refused"] / r["correct_n"] for r in reportable]
    assert max(lifts) <= 0.05, (
        f"a gate in retail v3 became informative (max lift {max(lifts):+.2f}). That would be "
        "good news and it contradicts the recorded finding — re-read before trusting it."
    )
