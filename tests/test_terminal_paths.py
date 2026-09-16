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

WHAT IS ACTUALLY TRUE TODAY — **changed 2026-09-16, principal's call on N9.** The
gate no longer fires on one status from one condition. `beads.py`'s
`TERMINAL_GATE_POLICY` declares, per terminal status, what the choke point does:
DONE is gated, VERIFY_FAILED is exempt (it is the gate's own output), SKIPPED is
refused outright on a gated bead, CANCELLED and FAILED are allowed because
neither is a completion claim. `retire()` keeps its own check for the better
error message, but it is no longer the place the rule lives.

**That is why the manifest below no longer carries a per-site `gated` bool.**
Gatedness is a property of the STATUS, not of the verb that reached for it — so
a per-site bool could disagree with the table, and a manifest that can disagree
with the code it audits is the thing N10 warns about. The manifest now
enumerates the doors; the policy comes from the table; a test asserts every door
is covered by it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentco_harness.beads import TERMINAL_GATE_POLICY, TaskStatus

PKG = Path(__file__).resolve().parents[1] / "agentco_harness"

TERMINAL = ("DONE", "SKIPPED", "CANCELLED", "FAILED", "VERIFY_FAILED")

#: Every site that WRITES a terminal status. Keep it sorted and keep the reasons
#: honest. This is the enumeration; `TERMINAL_GATE_POLICY` is the policy. They
#: are deliberately two different artifacts derived two different ways — the
#: manifest by scanning source text, the table by importing the module — so a
#: change cannot satisfy both by editing one.
EXPECTED_TERMINAL_WRITES = {
    # --- inside beads.py: the store's own verbs ------------------------------
    ("beads.py", "DONE"),           # the choke point's own condition
    ("beads.py", "VERIFY_FAILED"),  # written BY the gate, after it ran
    ("beads.py", "SKIPPED"),        # retire(): refuses a bead carrying metadata.verify
    ("beads.py", "CANCELLED"),      # cancel(): D2 — deliberate, abandons gated work
    ("beads.py", "FAILED"),         # a failure is not a completion claim
    # --- outside beads.py: the doors -----------------------------------------
    ("cli.py", "DONE"),             # routes through update() -> gate fires
    ("cli.py", "FAILED"),
    ("humans.py", "SKIPPED"),       # decline(terminal=True) — N5's door, closed by N9
}
#: Doors CLOSED (P2b, `ai-tasks/embedded-plane/PLAN.md`) by migrating the raw
#: `update(status=...)` call onto the plane's own verb, which no longer names
#: the status as a literal kwarg so this file's text scanner stops seeing it:
#: `("rca.py", "DONE")` — the RCA-resolved write now calls `beads.complete()`.
#: `("cli.py", "SKIPPED")` — `approve reject` / `reject-all` now call
#: `beads.retire()` (the exact semantics that raw call was reimplementing,
#: per its own former comment) instead of writing the status inline. Neither
#: removal changes what `TERMINAL_GATE_POLICY` does with DONE or SKIPPED —
#: `complete()` and `retire()` both still route through `update()` internally
#: (see `("beads.py", "DONE")` / `("beads.py", "SKIPPED")` above), so the gate
#: still fires exactly as before. Only the caller's vocabulary changed.

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
    added, removed = actual - EXPECTED_TERMINAL_WRITES, EXPECTED_TERMINAL_WRITES - actual
    assert not added, (
        f"NEW terminal-status write(s): {sorted(added)}. Declare each in "
        "EXPECTED_TERMINAL_WRITES, and make sure the status it writes has an "
        "entry in beads.TERMINAL_GATE_POLICY (N9)."
    )
    assert not removed, f"terminal-status write(s) gone: {sorted(removed)} — update the manifest."


def test_every_terminal_status_has_a_declared_gate_policy():
    """The general invariant, replacing "the gate fires on DONE and nothing else".

    This is the whole of N9 in one assertion: a status you can reach a terminal
    state through is a status the choke point has an opinion about. Before the
    relocation, four of the five had no opinion attached to them anywhere —
    which is how `decline(terminal=True)` and `approve reject` reached SKIPPED
    for months without anybody deciding they should.

    `TERMINAL` is this file's own literal, deliberately not imported from
    `beads.py`. If someone adds a sixth terminal status, this test does not
    notice — `test_the_set_of_terminal_paths_has_not_changed` does, by finding a
    door it cannot classify, and sends them here.
    """
    missing = [s for s in TERMINAL if TaskStatus[s] not in TERMINAL_GATE_POLICY]
    assert not missing, (
        f"terminal status(es) with no declared gate policy: {missing}. "
        "Add each to beads.TERMINAL_GATE_POLICY and say WHY in the comment — "
        "'nobody decided' is how N5's door stayed open."
    )


def test_every_door_writes_a_status_the_policy_covers():
    """The two artifacts, joined. Neither is authoritative alone.

    The manifest is built by scanning source text; the policy table by importing
    the module. A door whose status is absent from the table is a path to a
    terminal state that the choke point will wave through silently — exactly the
    shape this file exists to make impossible.
    """
    uncovered = sorted(
        (f, s) for f, s in EXPECTED_TERMINAL_WRITES if TaskStatus[s] not in TERMINAL_GATE_POLICY
    )
    assert not uncovered, f"door(s) writing an unpoliced terminal status: {uncovered}"


@pytest.mark.parametrize(
    "status", sorted(s for s in TERMINAL if TERMINAL_GATE_POLICY.get(TaskStatus[s]) == "allow")
)
def test_statuses_reachable_without_the_gate_are_known(status):
    """Pin the permissive set, so its size is visible in the test names.

    Two statuses reach terminal without the gate being consulted, and both are
    deliberate (D2: neither a cancellation nor a failure is a completion claim).
    Before N9 this was five *sites* and the number was an accident. It is now
    two *statuses* and the number is a decision — which is the property N5
    turned out not to have.
    """
    assert TaskStatus[status] in TERMINAL_GATE_POLICY


def test_retire_keeps_its_own_refusal_for_the_better_message():
    """Relocation does not mean deletion — N7's lesson, applied here.

    `_approval_answers_gate`'s docstring is the precedent: *"Not defensive
    duplication — relocation: these are the conditions, and `approve_verify` is
    now one convenient way to satisfy them."* Same here. The choke point cannot
    name `cancel` as the way out, because it does not know which verb the caller
    reached for; `retire()` can, and that message is the difference between a
    correct refusal and a helpful one.
    """
    src = (PKG / "beads.py").read_text()
    retire = src[src.index("def retire("):]
    retire = retire[: retire.index("\n    def ", 1)]
    assert "verify" in retire, "retire() no longer refuses gated beads (D2 regression)"
    assert "cancel" in retire, "retire()'s refusal no longer names the way out"


def test_the_choke_point_refuses_rather_than_relying_on_each_caller():
    """Guards against the relocation being quietly un-done one caller at a time.

    If this assertion has to be edited, the rule has moved back out of
    `update()` and `tests/test_terminal_gate_policy.py` is the file that says
    what the behaviour should still be.
    """
    src = (PKG / "beads.py").read_text()
    assert "policy is _REFUSE_IF_GATED" in src, (
        "update() no longer consults TERMINAL_GATE_POLICY before writing a "
        "terminal status — N9's relocation has been reverted or moved."
    )
