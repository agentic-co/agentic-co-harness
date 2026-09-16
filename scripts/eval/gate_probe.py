#!/usr/bin/env python3
"""Property-test every gate in an ASOP: does its check actually DISCRIMINATE?

    python3 scripts/eval/gate_probe.py evals/tau2-retail-asop/asops/*.md

WHY THIS EXISTS (finding N11). `EVIDENCE.md` records two candidate mechanisms
for C2 -- "a procedure improves when revised from gate evidence" -- aggregating
verdict statistics, or reading a transcript. Both need live runs. The PoC had
already found a third and better one, and the commit that did it spells out the
bug class:

    `ship-a-fix` steps 1-2 gated on a file being non-empty, so the string
    `everything passed fine` satisfied THE GATE THAT EXISTS TO PROVE THE TEST
    WENT RED BEFORE THE FIX. "That is the most important gate in the procedure
    and it checked nothing."

The revision method was adversarially probing the check expression against
CONSTRUCTED inputs, offline. No executor, no verdict corpus, no live run, and
-- the part that matters for sequencing -- **no dependency on C1**, which is why
C2 being recorded as blocked behind C1 was wrong.

WHAT THIS PROBES, precisely. A gate is a claim that some evidence is acceptable
and some is not. So feed it both and see whether it can tell them apart:

  * a gate that refuses everything is broken and will be noticed immediately;
  * a gate that ACCEPTS everything is broken and will never be noticed, because
    every run looks like a pass. That is the whole of the PoC's bug, and it is
    the class this script exists to find.

It runs `named_tool` and `check_tool_succeeded` from `asop_agent` -- the real
ones, imported, not reimplemented. A probe that reimplements the thing it
probes tests the reimplementation.

⚠️ THIS INHERITS `gate_reach.py`'s CEILING AND DOES NOT RAISE IT. What fires is
`check_tool_succeeded`: did this tool run, did it return ok. Discriminating
perfectly on that question is still a LIVENESS result. A gate reading
"deterministic (tool result shows status pending)" can earn a clean bill here
while remaining structurally blind to a status that is not pending. `DISCRIMINATES`
means "this check is a check", never "this check is sufficient".

WHAT THE THREE VERDICTS MEAN:
  DISCRIMINATES   passes the genuine-pass input and refuses every constructed
                  failure. The check is load-bearing.
  CHECKS NOTHING  there is no deterministic check to run (the gate declares one
                  and names no tool), so every input produces the same
                  non-answer. `gate_reach.py` calls these STRANDED; this script
                  reaches the same gates from the other direction, by behaviour
                  rather than by parsing.
  FALSE ACCEPT    the check passed an input it should have refused. The most
                  serious finding here: the gate looks rigorous, fires, reports
                  a deterministic verdict, and is wrong.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

_SPEC = importlib.util.spec_from_file_location(
    "asop_agent", Path(__file__).resolve().parent / "asop_agent.py"
)
asop_agent = importlib.util.module_from_spec(_SPEC)
sys.modules["asop_agent"] = asop_agent
_SPEC.loader.exec_module(asop_agent)

GateKind = asop_agent.GateKind


@dataclass(frozen=True)
class Probe:
    """One constructed tool history, and what the gate must say about it.

    `expect` is the verdict a correct check returns. The probe is interesting
    only where `expect` is False -- anybody can write a check that passes good
    evidence; the question is whether it refuses bad evidence.
    """

    name: str
    history: Callable[[str], tuple[str, ...]]
    expect: bool
    why: str


#: The probe table. Deliberately mirrors the shape of the PoC's, which read:
#:
#:     a pre-fix run that passed        -> refused
#:     prose with no recorded exit      -> refused
#:     a reproduction that exited zero  -> refused
#:     a genuine pre-fix failure        -> passes
#:
#: Each entry below is one way a run can LOOK like it satisfied the gate without
#: having done so. A gate that cannot refuse these is not checking anything, it
#: is agreeing with whatever it was handed.
PROBES: tuple[Probe, ...] = (
    Probe(
        "genuine pass",
        lambda t: (f"called {t}(user_id='u1')", "  -> ok: {'status': 'pending'}"),
        expect=True,
        why="the tool ran and returned ok — the one input that SHOULD pass",
    ),
    Probe(
        "tool ran and FAILED",
        lambda t: (f"called {t}(user_id='u1')", "  -> FAILED: user not found"),
        expect=False,
        why="the step's own tool reported failure",
    ),
    Probe(
        "tool never called",
        lambda t: ("called think(thought='I should check this')", "  -> ok: "),
        expect=False,
        why="nothing was done; a gate that passes here verifies the absence of work",
    ),
    Probe(
        "called, no result recorded",
        lambda t: (f"called {t}(user_id='u1')",),
        expect=False,
        why="the call was made but never came back — a hung or dropped call is not a success",
    ),
    Probe(
        "narration only, no call",
        lambda t: (
            "called think(thought='everything passed fine, the tool returned ok')",
            "  -> ok: ",
        ),
        expect=False,
        why="the executor SAYS it succeeded. This is the PoC's exact bug: a gate "
            "satisfied by the string 'everything passed fine'",
    ),
    Probe(
        "a different tool succeeded",
        lambda t: ("called get_user_details(user_id='u1')", "  -> ok: {}"),
        expect=False,
        why="some tool ran; not this one. A gate that passes here checks 'did anything happen'",
    ),
    Probe(
        "failed first, then a later unrelated ok",
        lambda t: (
            f"called {t}(user_id='u1')",
            "  -> FAILED: upstream timeout",
            "called think(thought='retrying')",
            "  -> ok: ",
        ),
        expect=False,
        why="the step's tool failed and something else later succeeded — a gate that "
            "scans for any 'ok' after the call passes a run that never recovered",
    ),
)


@dataclass
class GateResult:
    where: str
    tool: Optional[str]
    verdict: str                      # DISCRIMINATES | CHECKS NOTHING | FALSE ACCEPT | ACCEPTS SIBLINGS
    failures: tuple[tuple[str, str], ...] = ()   # (probe name, why it matters)
    siblings: tuple[str, ...] = ()               # other REAL tools that satisfy this gate


def _siblings_accepted(tool: str, vocabulary: tuple[str, ...]) -> tuple[str, ...]:
    """Which OTHER real tools satisfy this gate when they succeed.

    `check_tool_succeeded` matches with `tool in line`, so a gate naming a tool
    that is a substring of another is satisfied by that other one. Whether that
    is a defect or an affordance **depends entirely on the environment's actual
    tool vocabulary**, which is why this takes one instead of fabricating names.

    Fabricating a sibling (`f"{tool}_draft"`) proves nothing: with substring
    semantics it always matches, so the probe would flag every gate in every
    document forever — a check that fires on everything is the failure mode this
    script exists to name, and writing one into the script would be funny only
    once. An earlier draft of this file did exactly that and flagged all 15 of
    retail v3, including the one case the document's own sidecar had already
    reasoned through and declared intentional.
    """
    out = []
    for other in vocabulary:
        if other == tool:
            continue
        passed, _ = asop_agent.check_tool_succeeded(
            tool, (f"called {other}(user_id='u1')", "  -> ok: {}")
        )
        if passed:
            out.append(other)
    return tuple(out)


def probe_gate(step, where: str, vocabulary: tuple[str, ...] = ()) -> GateResult:
    """Run the probe table against one step's deterministic gate."""
    tool = asop_agent.named_tool(step)
    if tool is None:
        return GateResult(where, None, "CHECKS NOTHING")

    failures: list[tuple[str, str]] = []
    for probe in PROBES:
        passed, _reason = asop_agent.check_tool_succeeded(tool, probe.history(tool))
        if passed != probe.expect:
            failures.append((probe.name, probe.why))

    siblings = _siblings_accepted(tool, vocabulary) if vocabulary else ()

    if failures:
        # A check that refuses its own genuine-pass input is broken too, but
        # loudly: every run fails and somebody notices within the hour.
        # Accepting something it should refuse is the silent one, so it names
        # the verdict.
        accepted_bad = [f for f in failures if f[0] != "genuine pass"]
        return GateResult(
            where, tool, "FALSE ACCEPT" if accepted_bad else "REFUSES EVERYTHING",
            tuple(failures), siblings,
        )
    if siblings:
        return GateResult(where, tool, "ACCEPTS SIBLINGS", (), siblings)
    return GateResult(where, tool, "DISCRIMINATES", (), ())


def probe_document(path: Path, vocabulary: tuple[str, ...] = ()) -> list[GateResult]:
    """Every declared deterministic gate in one document, probed."""
    doc = asop_agent.parse_asop(path.read_text())
    return [
        probe_gate(step, f"{proc.name} · {step.title.strip()}", vocabulary)
        for proc in doc.procedures
        for step in proc.steps
        for kind in step.gate_kinds
        if kind is GateKind.DETERMINISTIC
    ]


def report(path: Path, vocabulary: tuple[str, ...] = ()) -> tuple[int, int, int, int]:
    """Probe one document and print it. Returns (good, nothing, siblings, bad)."""
    results = probe_document(path, vocabulary)
    good = [r for r in results if r.verdict == "DISCRIMINATES"]
    nothing = [r for r in results if r.verdict == "CHECKS NOTHING"]
    sibling = [r for r in results if r.verdict == "ACCEPTS SIBLINGS"]
    bad = [r for r in results if r.verdict in ("FALSE ACCEPT", "REFUSES EVERYTHING")]

    print(f"\n{path}")
    print(f"  deterministic gates declared : {len(results)}")
    print(f"  ✅ DISCRIMINATES              : {len(good)}")
    print(f"  ⛔ CHECKS NOTHING             : {len(nothing)}")
    print(f"  ⚠️  ACCEPTS SIBLINGS           : {len(sibling)}")
    print(f"  🛑 FALSE ACCEPT               : {len(bad)}")
    if not vocabulary:
        print("     (no --tools given: the sibling probe did not run. "
              "Reduced cover, not a pass.)")
    for r in good:
        print(f"       ok        {r.where}  ->  `{r.tool}`")
    for r in nothing:
        print(f"       NOTHING   {r.where}  (declares a check, names no tool — "
              f"every input gets the same non-answer)")
    for r in sibling:
        print(f"       SIBLINGS  {r.where}  ->  `{r.tool}`  also satisfied by: "
              f"{', '.join(r.siblings)}")
    for r in bad:
        print(f"       {r.verdict}  {r.where}  ->  `{r.tool}`")
        for name, why in r.failures:
            print(f"                     probe {name!r}: {why}")
    if sibling:
        print("  DECIDE, do not assume: a sibling match is a DEFECT when the other tool "
              "would not satisfy the step, and an AFFORDANCE when it would (retail's "
              "`find_user_id` accepts `_by_email`/`_by_name_zip` on purpose — see its "
              "`.NOTES.md`). Record which, in the document's sidecar.")
    return len(good), len(nothing), len(sibling), len(bad)


def _vocabulary(argv: list[str]) -> tuple[tuple[str, ...], list[str]]:
    """Pull `--tools a,b,c` or `--tools @path` out of argv."""
    if "--tools" not in argv:
        return (), argv
    i = argv.index("--tools")
    raw = argv[i + 1] if i + 1 < len(argv) else ""
    if raw.startswith("@"):
        raw = Path(raw[1:]).read_text()
    names = tuple(sorted({n.strip() for n in raw.replace("\n", ",").split(",") if n.strip()}))
    return names, argv[:i] + argv[i + 2:]


def main(argv: list[str]) -> int:
    vocabulary, paths = _vocabulary(argv)
    if not paths:
        print(__doc__)
        return 2
    totals = [0, 0, 0, 0]
    for arg in paths:
        for i, n in enumerate(report(Path(arg), vocabulary)):
            totals[i] += n
    good, nothing, sibling, bad = totals
    print(f"\nTOTAL  {good} discriminating · {nothing} checking nothing · "
          f"{sibling} accepting siblings · {bad} false-accepting "
          f"(of {good + nothing + sibling + bad} declared)")
    print("DISCRIMINATES means the check is a check. It is still a LIVENESS check "
          "— did the tool run and return ok — never a correctness one.")
    # A lint. A gate that checks nothing, or accepts evidence it should refuse,
    # is the failure this exists to catch. A sibling match is NOT counted as a
    # failure: it is a question for the document's author, and a lint that fails
    # on questions gets silenced.
    return 1 if (nothing or bad) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
