"""The compiled self-check, applied to a hand-revised (not recompiled) ASOP.

EXP-L.md: "the compiled self-check must pass for every candidate: find how
`sopbench_asop_compile.py`'s self-check / `asop_engine.parse_asop` validates a
document and apply the same checks (parse, every step names a callable tool,
step order consistent with the domain action graph)."

`sopbench_asop_compile.self_check()` takes the compiler's own `PlannedProcedure`
objects, which only exist right after a fresh compile. A reviser round produces
a *hand-edited* document with no `PlannedProcedure`s behind it, so this module
re-derives the same three checks directly from the PARSED document:

  1. it parses at all, and its routing table is non-empty and points only at
     procedures that exist (same as `self_check`'s routing checks);
  2. every step declaring a deterministic gate names a tool that is actually
     callable (`asop_engine.named_tool`, exposed in the domain's tool list, and
     never `internal_get_database` — the same three assertions `self_check`
     makes per step);
  3. within each procedure, a tool's transitive prerequisites (per the
     domain's dependency tables — `sopbench_asop_compile.Tables`) all appear
     earlier in the procedure (the `dirgraph_satisfied` check `self_check`
     added after v1 shipped with 5 of 20 bank procedures out of order).

Building `Tables` needs the SOPBench checkout importable (`env.task`,
`env.variables`) — run this under SOPBench's own venv
(`~/Code/SOPBench/.venv/bin/python`), matching every other script in this
programme that touches SOPBench internals.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

DEFAULT_SOPBENCH = Path.home() / "Code" / "SOPBench"
# scripts/eval/ (one level up from this scripts/eval/expl/ package) — where
# asop_engine.py, sopbench_asop_compile.py and sopbench_extract.py live.
SCRIPT_EVAL_DIR = Path(__file__).resolve().parents[1]


def _ensure_importable(sopbench: Path) -> None:
    for p in (str(SCRIPT_EVAL_DIR), str(sopbench)):
        if p not in sys.path:
            sys.path.insert(0, p)


def build_domain_tables(domain_name: str, sopbench: Path = DEFAULT_SOPBENCH):
    """A `sopbench_asop_compile.Tables` for `domain_name`, built the same way
    `compile_bank()` builds one — just without compiling a document from it.
    """
    _ensure_importable(sopbench)
    cwd = os.getcwd()
    os.chdir(sopbench)  # create_assistant() reads domain resources relative to SOPBench root
    try:
        from sopbench_extract import environment_verified_nodes  # noqa: E402
        from sopbench_asop_compile import Tables  # noqa: E402
        from env.task import create_assistant  # noqa: E402
        from env.variables import domain_assistant_keys  # noqa: E402

        domain = domain_assistant_keys[domain_name]
        action_descriptions = dict(domain.action_descriptions)
        assistant = create_assistant(domain_name, False, "prompt", None)
        exposed = {t["function"]["name"] for t in assistant["tools"]}
        env_nodes = environment_verified_nodes(action_descriptions.keys(), exposed)
        return Tables(
            required=dict(domain.action_required_dependencies),
            customizable=dict(domain.action_customizable_dependencies),
            links=dict(domain.constraint_links),
            processes=dict(domain.constraint_processes),
            env_nodes=env_nodes,
            exposed=exposed,
        )
    finally:
        os.chdir(cwd)


def self_check_candidate(markdown: str, tables, sopbench: Path = DEFAULT_SOPBENCH) -> list[str]:
    """The three checks described in the module docstring. Empty list = pass."""
    _ensure_importable(sopbench)
    from asop_engine import parse_asop, named_tool, GateKind  # noqa: E402

    problems: list[str] = []
    try:
        asop = parse_asop(markdown)
    except Exception as exc:  # noqa: BLE001 - a parse failure IS a self-check failure
        return [f"parse_asop failed: {exc}"]

    if not asop.routing:
        problems.append("ASOP.routing is empty")
    for phrase, target in asop.routing:
        if asop.procedure(target) is None:
            problems.append(f"routing phrase {phrase!r} points at unknown procedure {target!r}")

    for proc in asop.procedures:
        called: list[str] = []
        for step in proc.steps:
            tool = named_tool(step)
            if GateKind.DETERMINISTIC in step.gate_kinds:
                if tool is None:
                    problems.append(
                        f"{proc.name!r} step {step.number}: deterministic gate names no callable tool"
                    )
                elif tool not in tables.exposed:
                    problems.append(
                        f"{proc.name!r} step {step.number}: named tool {tool!r} is not exposed/callable"
                    )
                elif tool == "internal_get_database":
                    problems.append(
                        f"{proc.name!r} step {step.number} names internal_get_database as a gate tool"
                    )
            if tool:
                for need in tables.prereq_closure(tool):
                    if need in called or need == tool:
                        continue
                    if any(named_tool(s) == need for s in proc.steps):
                        problems.append(
                            f"{proc.name!r}: {tool} is emitted before {need}, which the domain's "
                            "action graph requires first — dirgraph_satisfied would fail"
                        )
                called.append(tool)

    return problems


def sopbench_importable(sopbench: Path = DEFAULT_SOPBENCH) -> bool:
    """Guard for tests/CI environments that don't have the SOPBench venv."""
    try:
        build_domain_tables("hotel", sopbench=sopbench)
        return True
    except Exception:
        return False
