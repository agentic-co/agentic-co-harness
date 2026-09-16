"""Every external `Beads.update()` call site, enumerated and reasoned.

D9 (principal, 2026-09-16): the twelve sites below **stay on `update()`**. They
are not a backlog and they are not laziness — they are the honest answer to a
real gap, and this file exists so that answer is a command rather than a
paragraph somebody stops believing.

WHY A GAP EXISTS AT ALL. Each of the plane's verbs does one of two things: touch
metadata (`annotate`), or drive a FRESH terminal transition through the gate
(`complete`, `report_result`, `retire`, `cancel`). **None writes a plain
non-lifecycle field on a bead that is already terminal, parked, or mid-flight.**
That is what these twelve do.

WHY NOT JUST MAP THEM ANYWAY. Because P2b tried, on the plan's own instruction,
and the instruction was wrong in a way that would have corrupted data.
`TERMINAL_GATE_POLICY[DONE]` fires on the *requested* status regardless of the
bead's *current* one, so re-issuing DONE on a parked gated bead re-runs the gate:

    after report_result:              awaiting_verify
    after second update(status=DONE): awaiting_verify

i.e. mapping `orchestrator.py`'s three result-writes onto `report_result` would
have quietly re-parked already-completed, already-verified work. **A forced
mapping that lies about intent is worse than no mapping**, which is the whole of
D9 in one sentence.

WHAT THIS FILE IS FOR. Not to hold the number at twelve forever — to make a
thirteenth *announce itself*. A new `update()` call outside `beads.py` fails
this test, and whoever added it has to come here and say which it is: a site
with no verb (add it, with a reason) or a site that should have used one
(use it). Same mechanism as `test_terminal_paths.py`, one layer out.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "agentco_harness"

#: `beads.update(` / `self.beads.update(`, excluding dict.update and friends.
#: The negative class before the name is what keeps `kwargs.update(`,
#: `metadata.update(` and `state.update(` out — they are dict writes, not
#: lifecycle writes, and counting them is how the "44 sites" figure in an
#: earlier plan came to be wrong.
_CALL = re.compile(r"(?:^|[^_.\w])(?:beads|self\.beads)\.update\(")

#: Every external call site, with WHY no verb fits. Keep the reasons honest: a
#: wrong reason here is worse than no manifest, because it reads as assurance.
EXPECTED_SITES = {
    # --- no verb writes a plain field on an ALREADY-TERMINAL bead -------------
    ("orchestrator.py", "result/metadata on a bead the caller already confirmed DONE; "
                        "re-issuing DONE would re-run the gate and re-park it"),
    # --- no verb expresses a metadata DELETE ---------------------------------
    ("orchestrator.py", "the chat lease's RELEASE — deletes metadata keys, and "
                        "`annotate` merges. D3 rehomed the acquire and missed this"),
    # --- no dependency verb exists at all ------------------------------------
    ("orchestrator.py", "writes blocked_by. `annotate` refuses it deliberately and the "
                        "plane's `declare` is operator registry config, not dependencies"),
    ("asop_store.py", "writes blocked_by — same gap as above. A contract question for "
                      "the Plane lane; do NOT invent a local verb"),
    # --- no verb writes these fields -----------------------------------------
    ("cli.py", "`tasks update` — a generic multi-field write (due_at, starts_at, "
               "estimate, blocked_by); no verb touches most of them"),
    ("cli.py", "`tasks retry` — a RESET from FAILED back to PENDING with result "
               "cleared. Not a completion; no verb resets"),
    ("cli.py", "bare assigned_to write; there is no `assign` verb"),
    ("cli.py", "bare actual_hours write; there is no verb for it"),
    ("rca.py", "writes `description`, a non-metadata field no verb touches"),
    # --- compound atomic write -----------------------------------------------
    ("humans.py", "decline: conditional PENDING-or-SKIPPED + clearing assigned_to + the "
                  "allow_human_reassign bypass, all atomic. Splitting it reopens "
                  "RCA ac-77459255 / ac-e5e5ba7b"),
}

#: How many calls each module makes. Separate from the reasons above because a
#: module can hold several sites of the same shape, and collapsing them would
#: hide a new one arriving next to an old one.
EXPECTED_COUNTS = {
    "orchestrator.py": 5,
    "cli.py": 4,
    "asop_store.py": 1,
    "humans.py": 1,
    "rca.py": 1,
}


def _sites() -> dict[str, int]:
    found: dict[str, int] = {}
    for path in sorted(PKG.glob("*.py")):
        if path.name == "beads.py":
            continue  # the choke point's own internals are not external callers
        n = sum(1 for line in path.read_text().splitlines() if _CALL.search(line))
        if n:
            found[path.name] = n
    return found


def test_no_new_external_update_call_site_appears_silently():
    """A thirteenth site must be declared, not discovered.

    If this fails with MORE calls in a module, someone reached for `update()`.
    That may be right — D9 says nine of these are the honest answer — but it has
    to be said out loud, here, with the reason. If it fails with FEWER, a site
    moved onto a verb: delete it here and say which verb in the commit.
    """
    actual = _sites()
    assert actual == EXPECTED_COUNTS, (
        f"external Beads.update() call sites changed.\n"
        f"  expected: {EXPECTED_COUNTS}\n"
        f"  actual:   {actual}\n"
        "Declare a new site in EXPECTED_SITES with WHY no plane verb fits, or — if "
        "one does — use it. D9: these are documented exceptions, not a backlog."
    )


def test_beads_py_is_excluded_on_purpose():
    """The scanner must not count the choke point's own internal calls.

    `complete()`, `retire()`, `cancel()` and `report_result()` all route through
    `update()` from inside `beads.py`. Counting those would make the manifest
    grow every time a verb is added, which is the opposite of what it is for.
    """
    assert "beads.py" not in _sites()
    assert _CALL.search("        return self.update(task_id, status=TaskStatus.DONE)") is None


@pytest.mark.parametrize("noise", [
    "    kwargs.update(derived)",
    "    metadata.update(changes)",
    "    state.update(fields)",
    "    env.update(_ZAI_MODEL_ENV)",
    "    body.update(changes)",
])
def test_dict_updates_are_not_counted(noise):
    """The "44 sites" figure in an earlier plan was wrong because it counted these.

    A manifest inflated by dict writes would make the real number unfindable, and
    a number nobody can check is the shape N10 keeps warning about.
    """
    assert _CALL.search(noise) is None


def test_every_declared_module_actually_has_a_site():
    """Guards the reverse error: a reason written for a module with no call.

    A manifest entry that describes nothing still reads as knowledge, and would
    survive the counts test above untouched.
    """
    modules = {m for m, _ in EXPECTED_SITES}
    assert modules == set(EXPECTED_COUNTS), (
        f"EXPECTED_SITES describes {sorted(modules)} but the counts cover "
        f"{sorted(EXPECTED_COUNTS)} — one of them is stale."
    )
