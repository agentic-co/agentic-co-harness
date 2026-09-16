"""SKIPPED is a VIEW, so every write of it must carry what the view derives from.

D11 (principal, 2026-09-16), resolving the open question that `SKIPPED` and
`CANCELLED` have no plane `WorkStatus`:

  * **CANCELLED is an OUTCOME** and earns a real plane status. D2 already said so
    — DONE asserts completion, cancellation is the opposite claim — and it is
    excluded from `outcomes_by_version` as neither success nor failure.
  * **SKIPPED is a VIEW** wearing a status. It is the fourth instance of D1's
    pattern: *the runtime writes a STATUS to change what a queue shows, and the
    plane has no status for it, because a status is an outcome and this is a
    view.*

WHAT THAT OBLIGES, and it is the whole content of this file. A view has to be
reconstructible from what is stored. If a bead can reach SKIPPED carrying
nothing that says WHY, the plane cannot express it as a view and the information
is simply lost at the boundary — at which point "it is a view" is a claim about
intent rather than about the data.

So: **every path to SKIPPED stamps provenance.** There are exactly two, and
`test_terminal_paths.py` is what guarantees there are exactly two.

This is cheap to satisfy today because P2b already routed the CLI's reject
commands through `retire()`, which stamps `metadata.retirement`. The point of
the test is that it stays true — a third path to SKIPPED, or a fourth, has to
bring its provenance with it rather than discovering later that it did not.
"""

from __future__ import annotations

import pytest

from agentco_harness import humans
from agentco_harness.beads import Beads, TaskStatus


@pytest.fixture
def beads(tmp_path):
    return Beads(tmp_path / "tasks.jsonl")


def test_retire_stamps_who_and_why(beads):
    """`retire()` — the administrative close for moot, ungated work."""
    task = beads.create("clean up the scratch dir", "d")
    retired = beads.retire(task.id, by="alex", reason="superseded by the new pipeline")

    assert retired.status is TaskStatus.SKIPPED
    stamp = retired.metadata["retirement"]
    assert stamp["by"] == "alex"
    assert stamp["reason"] == "superseded by the new pipeline"
    assert stamp["at"], "a view with no timestamp cannot be ordered against anything"


def test_terminal_decline_stamps_its_own_history(beads):
    """`decline(terminal=True)` — a kill-dated bead nobody will pick back up.

    Different provenance shape from `retire()` on purpose: a decline is a record
    of a PERSON putting work down, and the history is append-only because the
    same bead can be declined, requeued and declined again. Flattening it into
    `retirement` would lose that.
    """
    task = beads.create("write the migration note", "d")
    beads.update(task.id, assigned_to="human:alex")

    declined = humans.decline_task(beads, task.id, "kill-dated", terminal=True)

    assert declined.status is TaskStatus.SKIPPED
    history = declined.metadata["decline_history"]
    assert history[-1]["terminal"] is True
    assert history[-1]["reason"] == "kill-dated"
    assert history[-1]["at"]


def test_every_path_to_skipped_carries_provenance(beads):
    """The invariant itself, over both paths, asserted as one property.

    If a third path to SKIPPED appears, `test_terminal_paths.py` fails first —
    it enumerates the doors. This one says what a new door OWES: something a
    reader can use to answer "why is this skipped?" without the answer being
    "because someone set a status once".
    """
    a = beads.create("moot thing", "d")
    beads.retire(a.id, by="alex", reason="moot")

    b = beads.create("declined thing", "d")
    beads.update(b.id, assigned_to="human:alex")
    humans.decline_task(beads, b.id, "kill-dated", terminal=True)

    for task_id in (a.id, b.id):
        task = beads.get(task_id)
        assert task.status is TaskStatus.SKIPPED
        provenance = {"retirement", "decline_history"} & set(task.metadata or {})
        assert provenance, (
            f"{task_id} reached SKIPPED carrying no provenance. D11 says SKIPPED is a "
            f"VIEW, and a view must be reconstructible from what is stored — otherwise "
            f"the reason is lost at the plane boundary and 'it is a view' is a claim "
            f"about intent rather than about the data."
        )


def test_cancelled_records_an_outcome_not_a_view(beads):
    """CANCELLED's stamp is the other half of D11, and it is a different thing.

    Cancellation is an OUTCOME — it earns a real plane status — so what it
    records is authority and justification (who decided, and why), not queue
    bookkeeping. `cancel` already refuses without both, which is why this test
    reads the stamp rather than asserting the refusal (that lives in
    test_retire_cancel.py).
    """
    task = beads.create("migrate", "d", metadata={"verify": {"class": "judged", "check": "ok?"}})
    cancelled = beads.cancel(task.id, by="alex", reason="requirement withdrawn")

    assert cancelled.status is TaskStatus.CANCELLED
    stamp = cancelled.metadata["cancellation"]
    assert stamp["by"] == "alex" and stamp["reason"] == "requirement withdrawn" and stamp["at"]
    assert "retirement" not in cancelled.metadata, (
        "cancellation is not a retirement — conflating them is exactly the "
        "two-meanings-in-one-verb problem D2 split apart"
    )
