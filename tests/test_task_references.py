"""Referential validation at the bead write boundary (bead ac-694377f3).

Live incident, 2026-08-07:

    agentco tasks create --blocked-by 'ac-3f4dd6f2\\nac-b7063b2b'

A shell mangled two ids into ONE argument carrying an embedded newline. It was
stored verbatim, no task will ever have that id, and the bead was permanently
blocked with zero diagnostics — the same silent-deadlock class
``DependencyCycleError`` exists to prevent, through a different door.

Two properties are pinned here, and one non-property:

* format and existence are enforced at CREATE and UPDATE, loudly, naming the
  offending value, writing nothing on rejection;
* the READ side stays tolerant of dangling ids, because after this guard they
  can only come from a hand edit, a bad merge, or legacy data, and a read that
  crashes on those hides the whole queue instead of degrading one bead
  (that tolerance is pinned in test_beads.py / test_tempo.py);
* a quarantined line does NOT count as existing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from agentco_harness.beads import (
    Beads,
    DependencyCycleError,
    TaskReferenceError,
    TaskStatus,
    normalize_blockers,
    validate_task_id,
)
from agentco_harness.cli import main

# The exact corruption from the incident: two ids joined by a literal newline.
INCIDENT_ARG = "ac-3f4dd6f2\nac-b7063b2b"


def _node(tmp_path: Path, monkeypatch) -> Beads:
    root = tmp_path / "node"
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    monkeypatch.chdir(root)
    return Beads(root / "tasks.jsonl")


# --- format -----------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        INCIDENT_ARG,  # the incident, verbatim
        "ac-3f4dd6f2\n",  # trailing newline alone — why fullmatch, not match
        " ac-3f4dd6f2",  # leading whitespace
        "ac-3f4dd6f2 ",  # trailing whitespace
        "ac-3F4DD6F2",  # uppercase: uuid4().hex is lowercase, so this is a typo
        "ac-3f4dd6f",  # 7 chars
        "ac-3f4dd6f22",  # 9 chars
        "ac-zzzzzzzz",  # not hex
        "3f4dd6f2",  # missing prefix
        "",  # empty
        "ac-",  # prefix only
        None,  # wrong type
        12345678,  # wrong type
    ],
)
def test_malformed_ids_are_refused_by_format(bad):
    with pytest.raises(TaskReferenceError) as exc:
        validate_task_id("blocked_by", bad)
    # The message must name the offending value — "invalid id" is not actionable.
    assert repr(bad) in str(exc.value)


def test_create_refuses_the_incident_argument_and_writes_nothing(tmp_path, monkeypatch):
    beads = _node(tmp_path, monkeypatch)
    with pytest.raises(TaskReferenceError) as exc:
        beads.create("x", "d", blocked_by=[INCIDENT_ARG])
    assert repr(INCIDENT_ARG) in str(exc.value)
    assert beads.list() == []


def test_create_refuses_a_malformed_parent_and_writes_nothing(tmp_path, monkeypatch):
    beads = _node(tmp_path, monkeypatch)
    with pytest.raises(TaskReferenceError):
        beads.create("x", "d", parent_id="not-an-id")
    assert beads.list() == []


def test_a_bare_string_blocked_by_is_refused_not_iterated(tmp_path, monkeypatch):
    """`blocked_by="ac-aaaaaaaa"` would otherwise validate character by
    character and produce a baffling error about 'a'."""
    beads = _node(tmp_path, monkeypatch)
    real = beads.create("real", "d")
    with pytest.raises(TaskReferenceError) as exc:
        beads.create("x", "d", blocked_by=real.id)
    assert "must be a list" in str(exc.value)


# --- existence --------------------------------------------------------------


def test_create_refuses_a_wellformed_but_nonexistent_blocker(tmp_path, monkeypatch):
    """The subtler half: `ac-deadbeef` passes the format check and still names
    nothing, so it would block the bead forever."""
    beads = _node(tmp_path, monkeypatch)
    with pytest.raises(TaskReferenceError) as exc:
        beads.create("x", "d", blocked_by=["ac-deadbeef"])
    assert "ac-deadbeef" in str(exc.value)
    assert "does not exist" in str(exc.value)
    assert beads.list() == []


def test_create_refuses_a_wellformed_but_nonexistent_parent(tmp_path, monkeypatch):
    beads = _node(tmp_path, monkeypatch)
    with pytest.raises(TaskReferenceError) as exc:
        beads.create("x", "d", parent_id="ac-deadbeef")
    assert "ac-deadbeef" in str(exc.value)
    assert beads.list() == []


def test_a_partly_valid_blocker_list_is_refused_whole(tmp_path, monkeypatch):
    """No partial write: one bad id rejects the whole create, rather than
    silently dropping the bad edge (undisclosed data loss) or storing it."""
    beads = _node(tmp_path, monkeypatch)
    real = beads.create("real", "d")
    with pytest.raises(TaskReferenceError):
        beads.create("x", "d", blocked_by=[real.id, "ac-deadbeef"])
    assert [t.title for t in beads.list()] == ["real"]


def test_a_quarantined_line_does_not_count_as_existing(tmp_path, monkeypatch, capsys):
    """An unparseable record is never dispatched and never completes, so it can
    never release what it blocks. Pointing at one is the same permanent
    deadlock as pointing at nothing, and is refused identically."""
    beads = _node(tmp_path, monkeypatch)
    # A line with an unknown status: quarantined by _read_all, preserved on disk.
    with open(beads.path, "a") as f:
        f.write(
            json.dumps(
                {
                    "id": "ac-aaaaaaaa",
                    "title": "corrupt",
                    "description": "",
                    "status": "not-a-status",
                    "priority": 2,
                    "blocked_by": [],
                    "metadata": {},
                }
            )
            + "\n"
        )

    with pytest.raises(TaskReferenceError) as exc:
        beads.create("x", "d", blocked_by=["ac-aaaaaaaa"])
    assert "ac-aaaaaaaa" in str(exc.value)

    # And the quarantined line still survives, verbatim — rejection must not
    # cost us the corrupt record we were preserving.
    assert "not-a-status" in beads.path.read_text()


# --- valid graphs still work ------------------------------------------------


def test_a_valid_graph_is_accepted(tmp_path, monkeypatch):
    beads = _node(tmp_path, monkeypatch)
    goal = beads.create("goal", "d")
    first = beads.create("first", "d", parent_id=goal.id)
    second = beads.create("second", "d", parent_id=goal.id, blocked_by=[first.id])

    assert second.parent_id == goal.id
    assert second.blocked_by == [first.id]
    assert second.id not in {t.id for t in beads.ready()}
    beads.complete(first.id)
    assert second.id in {t.id for t in beads.ready()}


def test_update_accepts_valid_blockers_and_refuses_ghosts(tmp_path, monkeypatch):
    beads = _node(tmp_path, monkeypatch)
    a = beads.create("a", "d")
    b = beads.create("b", "d")

    assert beads.update(b.id, blocked_by=[a.id]).blocked_by == [a.id]

    with pytest.raises(TaskReferenceError):
        beads.update(b.id, blocked_by=["ac-deadbeef"])
    # Untouched — we never partially apply an update we refused.
    assert beads.get(b.id).blocked_by == [a.id]


def test_update_refuses_carrying_a_preexisting_ghost_forward(tmp_path, monkeypatch):
    """A bead that already carries a ghost (legacy data) cannot launder it
    through an update that merely re-writes the same list. --clear-blocked-by
    is the documented way out; that is why the CLI error says so."""
    beads = _node(tmp_path, monkeypatch)
    a = beads.create("a", "d")
    b = beads.create("b", "d")
    with pytest.raises(TaskReferenceError):
        beads.update(b.id, blocked_by=["ac-deadbeef", a.id])
    assert beads.update(b.id, blocked_by=[]).blocked_by == []


def test_clearing_blockers_needs_no_references(tmp_path, monkeypatch):
    beads = _node(tmp_path, monkeypatch)
    a = beads.create("a", "d")
    b = beads.create("b", "d", blocked_by=[a.id])
    assert beads.update(b.id, blocked_by=[]).blocked_by == []


def test_clearing_the_parent_is_allowed(tmp_path, monkeypatch):
    beads = _node(tmp_path, monkeypatch)
    goal = beads.create("goal", "d")
    child = beads.create("child", "d", parent_id=goal.id)
    assert beads.update(child.id, parent_id=None).parent_id is None


# --- self-reference ---------------------------------------------------------


def test_self_block_is_refused_as_a_cycle(tmp_path, monkeypatch):
    """ac-X blocked_by ac-X is well-formed AND exists, so it survives the
    referential checks and is caught by the cycle guard, which names it."""
    beads = _node(tmp_path, monkeypatch)
    a = beads.create("a", "d")
    with pytest.raises(DependencyCycleError):
        beads.update(a.id, blocked_by=[a.id])
    assert beads.get(a.id).blocked_by == []


def test_self_parent_is_refused(tmp_path, monkeypatch):
    """A task that parents itself makes _depth_of unresolvable and the goal
    lineage unreadable — the tree-edge twin of a self-block."""
    beads = _node(tmp_path, monkeypatch)
    a = beads.create("a", "d")
    with pytest.raises(TaskReferenceError) as exc:
        beads.update(a.id, parent_id=a.id)
    assert "its own parent" in str(exc.value)
    assert beads.get(a.id).parent_id is None


# --- duplicates: normalized, not rejected -----------------------------------


def test_duplicate_blockers_are_deduplicated_preserving_order():
    """DOCUMENTED DECISION: blocked_by is a SET of preconditions. Listing one
    twice expresses nothing a single entry does not, and ready() already reads
    it that way, so duplicates are normalized away rather than refused. Order
    is preserved because it is what `tasks show` and `me` display."""
    assert normalize_blockers(["ac-aaaaaaaa", "ac-bbbbbbbb", "ac-aaaaaaaa"]) == [
        "ac-aaaaaaaa",
        "ac-bbbbbbbb",
    ]


def test_create_dedups_duplicate_blockers(tmp_path, monkeypatch):
    beads = _node(tmp_path, monkeypatch)
    a = beads.create("a", "d")
    c = beads.create("c", "d", blocked_by=[a.id, a.id])
    assert c.blocked_by == [a.id]


def test_update_dedups_duplicate_blockers(tmp_path, monkeypatch):
    beads = _node(tmp_path, monkeypatch)
    a = beads.create("a", "d")
    b = beads.create("b", "d")
    c = beads.create("c", "d")
    assert beads.update(c.id, blocked_by=[a.id, b.id, a.id]).blocked_by == [a.id, b.id]


# --- the orchestrator / recurring shapes still work -------------------------


def test_the_sequential_subtask_chain_shape_still_works(tmp_path, monkeypatch):
    """The orchestrator's decompose path: children created under a parent, each
    chained to the previous sibling, then the parent gated on all of them."""
    beads = _node(tmp_path, monkeypatch)
    goal = beads.create("goal", "d")

    created_ids: list[str] = []
    for i in range(3):
        sub = beads.create(
            f"step {i}",
            "d",
            parent_id=goal.id,
            status=TaskStatus.PENDING_APPROVAL,
            blocked_by=[created_ids[-1]] if created_ids else None,
        )
        created_ids.append(sub.id)

    merged = list(dict.fromkeys((beads.get(goal.id).blocked_by or []) + created_ids))
    assert beads.update(goal.id, blocked_by=merged).blocked_by == created_ids


# --- CLI surface ------------------------------------------------------------


def test_cli_create_refuses_the_incident_argument(tmp_path, monkeypatch):
    runner = CliRunner()
    beads = _node(tmp_path, monkeypatch)

    result = runner.invoke(main, ["tasks", "create", "x", "--blocked-by", INCIDENT_ARG])
    assert result.exit_code == 1
    assert "not a valid task id" in result.output
    assert "Task NOT created" in result.output
    assert beads.list() == []


def test_cli_create_refuses_a_nonexistent_blocker(tmp_path, monkeypatch):
    runner = CliRunner()
    beads = _node(tmp_path, monkeypatch)

    result = runner.invoke(main, ["tasks", "create", "x", "--blocked-by", "ac-deadbeef"])
    assert result.exit_code == 1
    assert "ac-deadbeef" in result.output
    assert beads.list() == []


def test_cli_create_refuses_a_nonexistent_parent(tmp_path, monkeypatch):
    runner = CliRunner()
    beads = _node(tmp_path, monkeypatch)

    result = runner.invoke(main, ["tasks", "create", "x", "--parent", "ac-deadbeef"])
    assert result.exit_code == 1
    assert "ac-deadbeef" in result.output
    assert beads.list() == []


def test_cli_update_refuses_and_names_the_escape_hatch(tmp_path, monkeypatch):
    runner = CliRunner()
    beads = _node(tmp_path, monkeypatch)
    a = beads.create("a", "d")

    result = runner.invoke(
        main, ["tasks", "update", a.id, "--blocked-by", "ac-deadbeef"]
    )
    assert result.exit_code == 1
    assert "ac-deadbeef" in result.output
    assert "--clear-blocked-by" in result.output
    assert beads.get(a.id).blocked_by == []


def test_cli_create_echoes_the_stored_blockers_not_the_typed_ones(
    tmp_path, monkeypatch
):
    runner = CliRunner()
    beads = _node(tmp_path, monkeypatch)
    a = beads.create("a", "d")

    result = runner.invoke(
        main, ["tasks", "create", "c", "--blocked-by", a.id, "--blocked-by", a.id]
    )
    assert result.exit_code == 0, result.output
    assert result.output.count(a.id) == 1  # deduplicated in the confirmation
