"""ISC-1: the ISA-to-ASOP compiler, proven against a synthetic ISA.

Goal: *"A tool exists that takes an ISA's `## Criteria` (ISCs) and emits a
valid ASOP gate definition ... verified by passing asop-spec's conformance
vectors on the emitted output."* Principal's scoping call (2026-09-17): build
the compiler, prove it against a synthetic ISA, decide the real target domain
separately — no real domain's ISA is authored here.

TWO CONFORMANCE TARGETS, tested separately, because they are different claims:

1. Every emitted gate object passes `asop.gates.validate_gate()` — the SAME
   validator asop-spec's own conformance vectors exercise. This is the literal
   ISC-1 deliverable.
2. The emitted markdown round-trips through THIS repo's own
   `scripts/eval/asop_agent.py::parse_asop`, so `gate_reach.py` and
   `gate_probe.py` can immediately answer questions about it.

The two disagree on purpose for one criterion in the fixture (ISC-4: a raw
shell command has no named tool, so it is a valid asop-spec `deterministic`
gate that reads as `DETERMINISTIC_UNAVAILABLE` in this repo's tau2-style
runtime) — that disagreement is reported by the compiler, not hidden, and a
test pins that the report fires.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "scripts" / "algorithm" / "fixtures" / "sample-isa.md"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


isa_to_asop = _load("isa_to_asop", REPO / "scripts" / "algorithm" / "isa_to_asop.py")
asop_agent = _load("asop_agent", REPO / "scripts" / "eval" / "asop_agent.py")
from asop import gates as asop_gates  # noqa: E402


# -------------------------------------------------------------- parsing


def test_parses_every_isc_in_the_criteria_section():
    criteria = isa_to_asop.parse_isa(FIXTURE.read_text())
    assert [c.isc_id for c in criteria] == [f"ISC-{i}" for i in range(1, 6)]


def test_test_strategy_rows_are_joined_by_isc_id():
    criteria = {c.isc_id: c for c in isa_to_asop.parse_isa(FIXTURE.read_text())}
    assert criteria["ISC-1"].tool == "run_command"
    assert criteria["ISC-2"].tool == "human"
    assert "confirmed" in criteria["ISC-2"].probe.lower()


def test_a_criterion_with_no_row_and_no_verified_by_clause_has_no_probe():
    """ISC-5 in the fixture is deliberately bare — the input to the refusal test."""
    criteria = {c.isc_id: c for c in isa_to_asop.parse_isa(FIXTURE.read_text())}
    assert criteria["ISC-5"].probe is None
    assert criteria["ISC-5"].tool is None


# ---------------------------------------------------------- classification


def test_run_command_tool_classifies_deterministic():
    c = isa_to_asop.Criterion("ISC-X", "t", "d", probe="pytest -q", tool="run_command")
    kind, kwargs = isa_to_asop.classify(c)
    assert kind == "deterministic"
    assert kwargs["check"] == "pytest -q"


def test_human_marker_classifies_human_and_still_carries_a_check():
    """The gap this test exists to pin: a first draft of the compiler emitted
    `human` gates with no `check`, and asop.gates.validate_gate refuses that —
    'check'/'checks' is required on EVERY kind, human included ("the criteria
    to apply for a judged or human one"). Caught empirically, not by reading
    the docstring twice; this test is what stops it coming back.
    """
    c = isa_to_asop.Criterion("ISC-X", "t", "d", probe="the reviewer confirms X")
    kind, kwargs = isa_to_asop.classify(c)
    assert kind == "human"
    assert kwargs["check"]


def test_no_probe_and_no_expected_refuses_rather_than_guesses():
    c = isa_to_asop.Criterion("ISC-X", "t", "d")
    with pytest.raises(isa_to_asop.CompileError, match="cannot classify"):
        isa_to_asop.classify(c)


# --------------------------------------------------------- gate validation


def test_every_compilable_criterion_emits_a_spec_valid_gate():
    """The literal ISC-1 deliverable: run the SAME validator the spec's own
    conformance vectors exercise, on every gate this compiler emits."""
    compiled = isa_to_asop.compile_isa(_text_without_isc5())
    assert len(compiled) == 4
    for cg in compiled:
        # Re-validate independently of compile_isa's own internal call, so a
        # bug that made compile_isa call the wrong validator would still be
        # caught here.
        revalidated = asop_gates.validate_gate(cg.gate, require=())
        assert revalidated["kind"] == cg.gate["kind"]


def test_a_malformed_gate_is_refused_not_silently_stored():
    """Mirrors asop/gates.py's own posture one layer up: a criterion this
    compiler cannot turn into a complete gate must be refused at compile time,
    never stored as a gate that looks valid and checks nothing."""
    with pytest.raises(isa_to_asop.CompileError):
        isa_to_asop.compile_isa(FIXTURE.read_text())  # ISC-5 present, unfixed


# ------------------------------------------------------- markdown emission


_ISC5_LINE = (
    "- [ ] ISC-5: **A file's format is well-formed.** Present to prove "
    "refusal on ambiguity — this line names no test and has no Test "
    "Strategy row.\n"
)


def _text_without_isc5() -> str:
    """Remove only the ISC-5 checklist LINE, not everything after it.

    A first draft of this helper used `.split("- [ ] ISC-5:")[0]`, which
    truncates the whole file at that point — silently dropping the '##
    Test Strategy' table too, since it comes after ISC-5 in the fixture.
    That made ISC-1 lose its Test Strategy row and fall back to a 'judged'
    classification instead of 'deterministic', failing three tests for a
    reason that had nothing to do with the thing they were testing. Caught
    by running the suite, not by re-reading the helper.
    """
    text = FIXTURE.read_text()
    assert _ISC5_LINE in text, "fixture changed shape — update _ISC5_LINE"
    return text.replace(_ISC5_LINE, "")


def _compiled_no_isc5():
    return isa_to_asop.compile_isa(_text_without_isc5())


def test_the_emitted_markdown_round_trips_through_this_repos_own_parser():
    compiled = _compiled_no_isc5()
    doc = asop_agent.parse_asop(isa_to_asop.render_asop(compiled))
    steps = doc.procedures[0].steps
    assert len(steps) == 4
    assert steps[0].gate_kinds == (asop_agent.GateKind.DETERMINISTIC,)
    assert steps[1].gate_kinds == (asop_agent.GateKind.HUMAN,)
    assert steps[2].gate_kinds == (asop_agent.GateKind.JUDGED,)


def test_a_spec_valid_deterministic_gate_with_no_named_tool_downgrades_honestly():
    """The disagreement between the two conformance targets, made visible.

    ISC-4's probe is a raw shell command with no tool name in it — a valid
    asop-spec 'deterministic' gate (it re-runs a command) that this repo's
    tau2-style reader cannot re-check mid-conversation, because there is no
    tool call to look for in the transcript. The compiler must flag this
    rather than let 'deterministic' quietly mean two different things.
    """
    compiled = {cg.isc_id: cg for cg in _compiled_no_isc5()}
    isc4 = compiled["ISC-4"]
    assert isc4.gate["kind"] == "deterministic"          # spec-valid
    assert not isc4.markdown_matches_spec                # but downgrades on render
    assert isc4.note

    doc = asop_agent.parse_asop(isa_to_asop.render_asop(list(compiled.values())))
    isc4_step = doc.procedures[0].steps[3]
    assert isc4_step.gate_kinds == (asop_agent.GateKind.DETERMINISTIC_UNAVAILABLE,)


def test_a_clean_tool_call_probe_does_not_downgrade():
    """The contrast case: ISC-1's probe DOES name a tool, so 'deterministic'
    means the same thing in both artifacts."""
    compiled = {cg.isc_id: cg for cg in _compiled_no_isc5()}
    assert compiled["ISC-1"].markdown_matches_spec
    assert "`find_user_id`" in compiled["ISC-1"].markdown_gate_line


# ------------------------------------------------------------------- CLI


def test_cli_writes_both_artifacts_and_reports_the_downgrade_count(tmp_path, capsys):
    isa_path = tmp_path / "isa.md"
    isa_path.write_text(_text_without_isc5())

    rc = isa_to_asop.main([str(isa_path), "--out", str(tmp_path / "out")])
    out = capsys.readouterr().out

    assert rc == 0
    assert (tmp_path / "out" / "asop.generated.md").exists()
    assert (tmp_path / "out" / "gates.json").exists()
    assert "1 of 4 gate(s)" in out


def test_cli_refuses_loudly_on_an_uncompilable_isa(capsys):
    rc = isa_to_asop.main([str(FIXTURE)])  # ISC-5 present, unfixed
    err = capsys.readouterr().err
    assert rc == 1
    assert "REFUSED" in err and "ISC-5" in err
