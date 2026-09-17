#!/usr/bin/env python3
"""ISC-1: compile an ISA's `## Criteria` into ASOP gates, mechanically.

    python3 scripts/algorithm/isa_to_asop.py <isa.md> --out <dir>

WHY THIS EXISTS. Every ASOP tested in this programme so far — v1 through v6 —
was hand-authored: a human (or an agent standing in for one) read a policy in
prose and decided, by eye, which sentences deserved a gate and what kind. N14
found the consequence: ASOP-shaped documents carry ~2x the refusal language of
the prose they were extracted from, with ~25 preconditions and ~30 gates the
prose never had. That could be a property of the ASOP FORM — or it could be an
artifact of how a person hand-translating a policy tends to over-gate, out of
caution, in a way a mechanical compiler would not.

ISC-1 (`ai-tasks/lifeos-exit/ISA.md`) is the way to tell the two apart: *"A tool
exists that takes an ISA's `## Criteria` (ISCs) and emits a valid ASOP gate
definition ... plus the bead/step shape those criteria imply — verified by
passing asop-spec's conformance vectors on the emitted output."* A gate exists
only where an ISC demanded verification — never because a human felt a
sentence looked risky. This is that tool, and only that tool: no ISA is
authored here for any real domain. The principal's own scoping call
(2026-09-17): build the compiler first, prove it against a synthetic ISA, and
decide the target domain (SOPBench? retail tau2? something else?) separately,
once the mechanism is shown to work at all.

TWO ARTIFACTS, TWO CONFORMANCE TARGETS, because the phrase "passing asop-spec's
conformance vectors" and "an ASOP this repo's own runtime can execute" are not
the same claim:

1. **A gate object per ISC** — `{kind, check, ...}` — run through
   `asop.gates.validate_gate()`, the SAME validator the spec's conformance
   vectors exercise. This is the literal ISC-1 deliverable and is checked here,
   not merely hoped for: `compile_isa()` raises if any emitted gate is refused.

2. **An ASOP markdown document**, in the syntax `scripts/eval/asop_agent.py`
   already parses (`## Procedure` sections, numbered steps, `Gate: <kind>
   (...)` lines), so the compiler's output is something `gate_reach.py` and
   `gate_probe.py` can immediately answer questions about, and something
   `run_arm_c.py` could execute, without inventing a second document format.

**These two do not always agree, and the disagreement is reported rather than
hidden.** asop-spec's `deterministic` gate means "re-run this shell command."
This repo's markdown convention (built for tau2, a conversational agent with no
shell) means "did the agent call this named tool and did it return ok" — a
narrower, liveness-only reading of the same word. A criterion whose probe is a
raw command with no recognisable tool name compiles to a valid asop-spec
`deterministic` gate (artifact 1 passes) that renders as `DETERMINISTIC_UNAVAILABLE`
in the markdown (artifact 2, correctly, per `gate_reach.py`'s own logic) —
that is not a bug in the compiler; it is an honest statement that this
runtime's tau2 adapter cannot re-run an arbitrary shell command mid-conversation.
The compiler prints this rather than silently downgrading, which is the same
posture N8 already established for the hub's environment pins.

CLASSIFICATION RULE — deliberately mechanical, so it needs no judgment call per
criterion. Reads the ISA's `## Test Strategy` table if present (columns: ISC |
type | probe | expected | tool), else falls back to parsing the checklist
item's "verified by ..." clause with the same three buckets:

    tool == "run_command"                       -> deterministic
    tool/type mentions human/principal/reviewer  -> human
    everything else                              -> judged (probe -> check,
                                                    expected -> rubric)

A criterion the rule cannot classify with confidence is refused, loudly, at
compile time — never silently guessed into the most permissive bucket. That is
the same "malformed gate refused at the write boundary, never stored" posture
`asop/gates.py` documents for itself, applied one layer up.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # for `asop` if editable-installed elsewhere
from asop import gates as asop_gates  # noqa: E402


class CompileError(ValueError):
    """A criterion could not be compiled into a gate. Refuse, don't guess."""


@dataclass(frozen=True)
class Criterion:
    """One ISC, plus whatever its Test Strategy row adds."""

    isc_id: str
    title: str
    description: str
    probe: Optional[str] = None
    expected: Optional[str] = None
    tool: Optional[str] = None
    isc_type: Optional[str] = None


@dataclass(frozen=True)
class CompiledGate:
    isc_id: str
    title: str
    gate: dict                       # validated asop-spec gate object
    markdown_gate_line: str          # this repo's `Gate: ...` rendering
    markdown_matches_spec: bool      # False when artifact 2 downgrades vs artifact 1
    note: str = ""


# ── parsing the ISA ──────────────────────────────────────────────────────────

_ISC_RE = re.compile(
    r"^-\s*\[[ x]\]\s*(ISC-[\w-]+):\s*\*\*(.+?)\*\*\s*(.*)$", re.M
)
_TEST_STRATEGY_ROW_RE = re.compile(
    r"^\|\s*(ISC-[\w-]+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*$",
    re.M,
)
_VERIFIED_BY_RE = re.compile(r"verified by\s+(.+?)\.?\s*$", re.I)


def _section(markdown: str, heading: str) -> str:
    m = re.search(rf"^##\s+{re.escape(heading)}\s*$", markdown, re.M)
    if not m:
        return ""
    start = m.end()
    nxt = re.search(r"^##\s+", markdown[start:], re.M)
    return markdown[start : start + nxt.start()] if nxt else markdown[start:]


def parse_isa(markdown: str) -> list[Criterion]:
    """Every `## Criteria` item, joined with its `## Test Strategy` row if any."""
    criteria_body = _section(markdown, "Criteria")
    if not criteria_body.strip():
        raise CompileError("no '## Criteria' section — is this an ISA?")

    strategy_rows: dict[str, tuple[str, str, str, str]] = {}
    for m in _TEST_STRATEGY_ROW_RE.finditer(_section(markdown, "Test Strategy")):
        isc_id, isc_type, probe, expected, tool = (g.strip() for g in m.groups())
        if isc_id.lower() == "isc" or isc_type.lower() == "type":
            continue  # header row
        strategy_rows[isc_id] = (isc_type, probe, expected, tool)

    out: list[Criterion] = []
    for m in _ISC_RE.finditer(criteria_body):
        isc_id, title, rest = m.group(1), m.group(2), m.group(3).strip()
        if isc_id in strategy_rows:
            isc_type, probe, expected, tool = strategy_rows[isc_id]
        else:
            isc_type = probe = expected = tool = None
            vb = _VERIFIED_BY_RE.search(rest)
            if vb:
                probe = vb.group(1).strip()
        out.append(Criterion(
            isc_id=isc_id, title=title, description=rest,
            probe=probe, expected=expected, tool=tool, isc_type=isc_type,
        ))
    if not out:
        raise CompileError("'## Criteria' section has no ISC-N checklist items")
    return out


# ── classification ───────────────────────────────────────────────────────────

_HUMAN_MARKERS = re.compile(r"\bhuman\b|\bprincipal\b|\breviewer\b|\bapprover\b", re.I)
#: The same tool-name pattern `scripts/eval/asop_agent.py::named_tool` uses, so
#: the markdown side of this compiler and the runtime's own reachability check
#: agree on what counts as a re-runnable tool call.
_TOOL_NAME_RE = re.compile(r"`([a-z_][a-z0-9_]*)`|\b([a-z_]+_[a-z_]+)\b")


def _named_tool(text: str) -> Optional[str]:
    for hit in _TOOL_NAME_RE.finditer(text or ""):
        name = hit.group(1) or hit.group(2)
        if name:
            return name
    return None


def classify(c: Criterion) -> tuple[str, dict]:
    """One ISC -> (asop-spec kind, gate kwargs). Refuses rather than guesses.

    Precedence: an explicit `tool` column from the Test Strategy row is
    authoritative when present, because it is the author's own classification,
    not an inference from prose. Absent that, prose markers decide.
    """
    haystack = " ".join(filter(None, (c.tool, c.isc_type, c.probe, c.expected, c.description)))

    if c.tool and c.tool.strip().lower() == "run_command":
        if not c.probe:
            raise CompileError(f"{c.isc_id}: tool=run_command but no probe to run")
        return "deterministic", {"check": c.probe.strip()}

    if _HUMAN_MARKERS.search(haystack):
        # `asop.gates.validate_gate` requires `check`/`checks` on EVERY kind,
        # human included: "the criteria to apply for a judged or human one."
        # A human gate with nothing to check is a status field wearing a gate.
        check = c.probe or c.expected or c.description
        if not check:
            raise CompileError(f"{c.isc_id}: human gate has nothing for the reviewer to apply")
        return "human", {"check": check.strip()}

    if c.tool and c.tool.strip().lower() in {"view_file", "read_file"}:
        # A file inspection is not re-runnable as a pass/fail command in the
        # general case (its content is the thing under judgment), so it is
        # judged with the probe as the check text — never silently promoted
        # to deterministic just because a file path looks concrete.
        return "judged", {"check": c.probe or c.description, "rubric": c.expected}

    if c.probe or c.expected:
        return "judged", {"check": c.probe or c.description, "rubric": c.expected}

    raise CompileError(
        f"{c.isc_id}: cannot classify — no Test Strategy row and no 'verified by' "
        f"clause in the checklist item. A criterion with no probe is not hard-to-vary "
        f"(IsaFormat.md's own definition) and cannot compile to a gate."
    )


# ── emission ──────────────────────────────────────────────────────────────────

def _markdown_gate_line(kind: str, gate: dict, c: Criterion) -> tuple[str, bool]:
    """This repo's `Gate: ...` rendering, and whether it MATCHES the spec kind.

    Returns (line, matches). `matches=False` marks the honest downgrade case:
    the JSON gate is `deterministic` per asop-spec, but the markdown reader
    (`asop_agent.py`, built for a tool-calling conversational agent) has no
    named tool to re-check, so gate_reach.py will correctly report this step
    as DETERMINISTIC_UNAVAILABLE. That is not a compiler defect — see the
    module docstring — but a caller pooling "deterministic gates emitted" with
    "deterministic gates that will actually fire" would be repeating N8's
    mistake, so the flag makes the gap visible in the return value itself
    rather than only in a comment.
    """
    if kind == "human":
        return f"Gate: human ({gate['check']}).", True
    if kind == "deterministic":
        tool = _named_tool(gate.get("check") or "")
        if tool:
            return f"Gate: deterministic (tool call: `{tool}`).", True
        return f"Gate: deterministic (checks: {gate['check']}).", False
    # judged
    check = gate.get("check") or c.description
    return f"Gate: judged ({check}).", True


def compile_criterion(c: Criterion) -> CompiledGate:
    kind, kwargs = classify(c)
    payload = {"kind": kind, **kwargs}
    try:
        gate = asop_gates.validate_gate(payload, require=())
    except Exception as e:  # asop.gates._refuse raises a typed error; catch broadly, report exactly
        raise CompileError(f"{c.isc_id}: emitted gate refused by asop-spec — {e}") from e

    line, matches = _markdown_gate_line(kind, gate, c)
    note = "" if matches else (
        "spec kind is 'deterministic' (a re-runnable command) but no named tool "
        "was found in the probe text, so this repo's tau2-style reader will treat "
        "it as unavailable — see the module docstring's 'two conformance targets'."
    )
    return CompiledGate(isc_id=c.isc_id, title=c.title, gate=gate,
                        markdown_gate_line=line, markdown_matches_spec=matches, note=note)


def compile_isa(markdown: str) -> list[CompiledGate]:
    """The whole tool, in one call: parse, classify, validate, render."""
    return [compile_criterion(c) for c in parse_isa(markdown)]


def render_asop(compiled: list[CompiledGate], *, procedure_name: str = "Generated Procedure") -> str:
    """One flat procedure, one step per ISC, in the syntax `asop_agent.py` parses.

    Deliberately the simplest possible shape — one procedure, no routing, no
    conditional steps, no multiplicity. Grouping criteria into several
    procedures, or deriving branching, is a real design question ISC-1's own
    text leaves open ("the bead/step shape those criteria imply") and is
    NOT decided here: the principal scoped this build to the compiler and a
    synthetic ISA, not to a real document's step architecture.
    """
    lines = [f"# {procedure_name}", "", f"## {procedure_name}", ""]
    for i, cg in enumerate(compiled, start=1):
        lines.append(f"{i}. **{cg.title}** ({cg.isc_id}). {cg.markdown_gate_line}")
    return "\n".join(lines) + "\n"


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("isa", type=Path)
    ap.add_argument("--out", type=Path, default=None, help="dir to write asop.generated.md + gates.json")
    args = ap.parse_args(argv)

    try:
        compiled = compile_isa(args.isa.read_text())
    except CompileError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1

    print(f"{len(compiled)} criteria compiled:")
    downgraded = 0
    for cg in compiled:
        flag = "" if cg.markdown_matches_spec else "  ⚠️ markdown downgrade"
        print(f"  {cg.isc_id:<8} {cg.gate['kind']:<13} {cg.markdown_gate_line}{flag}")
        if not cg.markdown_matches_spec:
            downgraded += 1
    if downgraded:
        print(f"\n{downgraded} of {len(compiled)} gate(s) are spec-valid 'deterministic' but will read as "
              f"unavailable in this repo's tau2-style runtime — not a defect, see the module docstring.")

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "asop.generated.md").write_text(render_asop(compiled))
        import json
        (args.out / "gates.json").write_text(
            json.dumps({cg.isc_id: cg.gate for cg in compiled}, indent=2)
        )
        print(f"\nwrote {args.out / 'asop.generated.md'} and {args.out / 'gates.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
