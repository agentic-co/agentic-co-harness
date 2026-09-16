#!/usr/bin/env python3
"""Which gates in an ASOP can actually fire deterministically -- before running it.

WHY THIS EXISTS. "The deterministic gate path exists and has never fired" was
recorded as an open defect and repeated across four documents for days. It was
true of ASOP v1 and false of v3, v4 and v5, which fired 58 genuine
`deterministic-check` verdicts between them. Nobody knew, because the only way to
find out was to run a two-hour eval and read a counter -- and the counter that got
read (`fell_back`) is False for an ordinary JUDGED gate too, so it overstates the
answer in the other direction.

This answers the question statically, in a second, from the document alone:

    python3 scripts/eval/gate_reach.py evals/tau2-retail-asop/asops/*.md

A gate fires deterministically only when BOTH hold:
  1. it declares `deterministic` with "tool" or "api" in the parenthetical --
     otherwise `_gate_kinds` downgrades it to DETERMINISTIC_UNAVAILABLE; and
  2. `named_tool` finds a snake_case identifier in the gate text or step body --
     otherwise there is nothing to re-run and it downgrades at execution time.

Declaring (1) without (2) is the trap, and it is silent: the document LOOKS
rigorous, the run reports `deterministic_unavailable`, and the number everyone
quotes as deterministic is a model's opinion.

⚠️ LIVENESS, NOT CORRECTNESS. What fires is `check_tool_succeeded`: did this tool
run, and did it return ok. A gate reading "deterministic (tool result shows status
pending)" does NOT verify the status is pending -- only that the lookup ran. That
is a liveness check, structurally blind to semantic wrongness, and per phase-2.md
a liveness result must never be reported as a correctness result. This script
prints the distinction rather than letting a reader assume it away.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "asop_agent", Path(__file__).resolve().parent / "asop_agent.py"
)
asop_agent = importlib.util.module_from_spec(_SPEC)
sys.modules["asop_agent"] = asop_agent
_SPEC.loader.exec_module(asop_agent)

GateKind = asop_agent.GateKind


def report(path: Path) -> tuple[int, int]:
    """Print one document's gate reachability. Returns (firing, declared)."""
    doc = asop_agent.parse_asop(path.read_text())
    firing: list[tuple[str, str, str]] = []
    stranded: list[tuple[str, str]] = []
    other: dict[str, int] = {}

    for proc in doc.procedures:
        for step in proc.steps:
            for kind in step.gate_kinds:
                if kind is GateKind.DETERMINISTIC:
                    tool = asop_agent.named_tool(step)
                    where = f"{proc.name} · {step.title.strip()}"
                    if tool:
                        firing.append((where, tool, step.title.strip()))
                    else:
                        stranded.append((where, "declares a tool gate, names no tool"))
                else:
                    other[kind.value] = other.get(kind.value, 0) + 1

    declared = len(firing) + len(stranded)
    print(f"\n{path}")
    print(f"  deterministic gates declared : {declared}")
    print(f"  ✅ WILL FIRE (liveness)       : {len(firing)}")
    print(f"  ⛔ STRANDED -> judged         : {len(stranded)}")
    for kind, count in sorted(other.items()):
        print(f"     {kind:<28} : {count}")
    for where, tool, _ in firing:
        print(f"       fires  {where}  ->  `{tool}`")
    for where, why in stranded:
        print(f"       STRANDED  {where}  ({why})")
    if stranded:
        print("  FIX: name the tool in the gate, e.g. "
              "`Gate: deterministic (tool call: get_order_details)`.")
    return len(firing), declared


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    total_fire = total_declared = 0
    for arg in argv:
        fire, declared = report(Path(arg))
        total_fire += fire
        total_declared += declared
    print(f"\nTOTAL  {total_fire} of {total_declared} declared deterministic gates "
          f"can fire. The rest are judged gates wearing a deterministic label.")
    print("Every firing gate above is a LIVENESS check (did the tool run and "
          "return ok), never a correctness check. Do not pool the two.")
    # Non-zero when a document strands a gate: this is a lint, and a stranded
    # gate is the failure it exists to catch.
    return 1 if total_fire < total_declared else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
