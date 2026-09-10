"""P1: the seam exists, is honest, and is the only construction point.

A seam nobody goes through is decoration, so the last test here is the one
that matters: it asserts no module reaches past the factory to build a store
directly. That is what makes P2 a one-line substitution instead of a hunt.
"""

from __future__ import annotations

import pathlib
import re

from agentco_harness.beads import Beads
from agentco_harness.lifecycle import Lifecycle, open_lifecycle

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "agentco_harness"


def test_the_current_implementation_satisfies_the_contract():
    """Structural, not inherited: Beads does not know Lifecycle exists."""
    assert isinstance(Beads, type)
    assert issubclass(Beads, Lifecycle)


def test_the_factory_returns_something_satisfying_the_contract(tmp_path):
    lifecycle = open_lifecycle(tmp_path / "tasks.jsonl")
    assert isinstance(lifecycle, Lifecycle)


def test_the_factory_is_a_working_store_not_a_stub(tmp_path):
    lifecycle = open_lifecycle(tmp_path / "tasks.jsonl")
    task = lifecycle.create("x", "d")
    assert lifecycle.get(task.id).title == "x"
    assert [t.id for t in lifecycle.list()] == [task.id]


def test_nothing_constructs_a_store_behind_the_seam():
    """The assertion P2 depends on. If this fails, someone added a call site
    that P2's substitution will silently miss — and a lifecycle that is
    half-substituted is two implementations again, which is the thing this
    whole phase exists to end."""
    offenders = {}
    for path in PACKAGE.glob("*.py"):
        if path.name in {"beads.py", "lifecycle.py"}:
            continue  # the implementation, and the seam itself
        hits = [
            line.strip()
            for line in path.read_text().splitlines()
            if re.search(r"\bBeads\s*\(", line)
        ]
        if hits:
            offenders[path.name] = hits
    assert not offenders, f"construct via open_lifecycle() instead: {offenders}"
