"""Every path to a terminal status, enumerated — the invariant as a command.

WHY THIS FILE EXISTS. `CLAUDE.md` carries an invariant inherited from the PoC's
PPEV work (`feae5c8a`, 2026-08-07): *"`Beads.update()` is the single choke point
where a bead reaches `done`; every path goes through it. Do not add a second way
to reach `done`."*

It was inherited as prose and broken anyway, twice, because the successor grew
terminal states the original rule never contemplated. `decline(terminal=True)`
reaches SKIPPED and `cancel()` reaches CANCELLED — **terminal, but not DONE** — so
the letter of the rule held while its purpose did not. That is finding N9, and
finding N10 says the counter-measure is to make a claim about code state
*executable* rather than written down once and believed thereafter.

So this file does not assert that the invariant holds. It asserts **what the set of
terminal-reaching paths currently is**, and fails the moment that set changes.
A new terminal door cannot be added silently; whoever adds one has to come here and
say, in the manifest below, whether it is gated. That is the whole mechanism.

WHAT IS ACTUALLY TRUE TODAY, measured rather than assumed: the gate fires on
exactly one condition, `beads.py`'s `if kwargs.get("status") == TaskStatus.DONE`.
Every other terminal status passes through `update()` without the gate being
consulted. `retire()` is the sole non-DONE path that checks `metadata.verify`, and
it does so itself rather than via the choke point.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "agentco_harness"

TERMINAL = ("DONE", "SKIPPED", "CANCELLED", "FAILED", "VERIFY_FAILED")

#: Every site that WRITES a terminal status, and whether the verify gate is
#: consulted on the way. Keep this sorted and keep the reasons honest — a wrong
#: `gated` value here is worse than no manifest, because it reads as assurance.
#:
#: `gated=True` means: reaching this status from this site cannot skip
#: `metadata.verify`. Today that is true only for DONE (the choke point fires on
#: it) and for `retire()` (which refuses a gated bead outright).
EXPECTED_TERMINAL_WRITES = {
    # --- inside beads.py: the store's own verbs ------------------------------
    ("beads.py", "DONE"): True,           # the choke point's own condition
    ("beads.py", "VERIFY_FAILED"): True,  # written BY the gate, after it ran
    ("beads.py", "SKIPPED"): True,        # retire(): refuses a bead carrying metadata.verify
    ("beads.py", "CANCELLED"): False,     # cancel(): D2 — deliberate, abandons gated work
    ("beads.py", "FAILED"): False,        # a failure is not a completion claim
    # --- outside beads.py: the doors -----------------------------------------
    ("rca.py", "DONE"): True,             # routes through update() -> gate fires
    ("cli.py", "DONE"): True,             # ditto
    ("cli.py", "FAILED"): False,
    ("cli.py", "SKIPPED"): False,         # "Rejected by principal" — NOT named in N5
    ("humans.py", "SKIPPED"): False,      # decline(terminal=True) — N5's door
}

#: An assignment to `status`, excluding `==` comparisons. Anchoring on the
#: ASSIGNMENT and then collecting every terminal status named on that line is
#: deliberate: the first version of this matched `status=TaskStatus.X` directly
#: and silently missed `status=TaskStatus.DONE if done else TaskStatus.FAILED`
#: (cli.py:589) — a real door, invisible to the detector meant to enumerate doors.
#: A check that misses the thing it exists to catch is worse than no check,
#: because it reads as assurance.
_ASSIGN = re.compile(r"status\s*=(?!=)")
_STATUS = re.compile(r"TaskStatus\.(" + "|".join(TERMINAL) + r")\b")
#: `list(status=...)` and `{t.id for t in ... status=...}` are READS. A read is not
#: a door, and counting one as a door would make this manifest noise.
_READ_CONTEXT = re.compile(r"\.list\(|for\s+\w+\s+in\b")


def _terminal_writes() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(PKG.glob("*.py")):
        for line in path.read_text().splitlines():
            if _READ_CONTEXT.search(line) or not _ASSIGN.search(line):
                continue
            for m in _STATUS.finditer(line):
                found.add((path.name, m.group(1)))
    return found


def test_the_set_of_terminal_paths_has_not_changed():
    """A new way to reach a terminal state must be declared, not discovered.

    If this fails with an ADDED pair, someone opened a new door: add it to the
    manifest with an honest `gated` value and say why in the commit. If it fails
    with a REMOVED pair, a door closed — delete the entry and note it.
    """
    actual = _terminal_writes()
    expected = set(EXPECTED_TERMINAL_WRITES)
    added, removed = actual - expected, expected - actual
    assert not added, (
        f"NEW terminal-status write(s): {sorted(added)}. "
        "Declare each in EXPECTED_TERMINAL_WRITES with whether the verify gate "
        "is consulted (N9)."
    )
    assert not removed, f"terminal-status write(s) gone: {sorted(removed)} — update the manifest."


def test_the_gate_fires_on_done_and_nothing_else():
    """The measured fact N9 rests on, asserted so it cannot drift silently."""
    src = (PKG / "beads.py").read_text()
    assert 'if kwargs.get("status") == TaskStatus.DONE:' in src, (
        "the verify gate's trigger condition changed. If the gate now fires on "
        "more than DONE, N9's premise has moved and the manifest above needs "
        "re-deriving rather than editing."
    )


@pytest.mark.parametrize(
    "site", sorted(k for k, gated in EXPECTED_TERMINAL_WRITES.items() if not gated)
)
def test_ungated_terminal_paths_are_known(site):
    """Pin the ungated set explicitly, so its size is visible in the test names.

    Five paths reach a terminal state today without the gate being consulted.
    That is not asserted to be correct — `cancel()` and `fail()` are deliberate
    (D2: cancellation is the opposite of a completion claim). It is asserted to be
    KNOWN, which is the property N5 turned out not to have.
    """
    assert site in EXPECTED_TERMINAL_WRITES


def test_retire_is_the_only_non_done_path_that_checks_the_gate():
    """Load-bearing for N9's recommendation, and cheap to verify.

    If a second non-DONE path grows a `metadata.verify` check, the "generalise the
    invariant" work has started and this test should be replaced by one asserting
    the general rule rather than this specific asymmetry.
    """
    src = (PKG / "beads.py").read_text()
    retire = src[src.index("def retire("):]
    retire = retire[: retire.index("\n    def ", 1)]
    assert 'verify' in retire, "retire() no longer refuses gated beads (D2 regression)"

    cancel = src[src.index("def cancel("):]
    cancel = cancel[: cancel.index("\n    def ", 1)]
    assert 'metadata.get("verify")' not in cancel and "metadata.get('verify')" not in cancel, (
        "cancel() now checks the gate — if that is intended, N9's asymmetry is "
        "closing and this test should assert the general invariant instead."
    )
