"""Beads tolerance: quarantine of bad lines, status enum, forward-compat."""

from __future__ import annotations

import json
import uuid

import pytest

from agentco_harness.beads import (
    Beads,
    Task,
    TaskStatus,
    DepthLimitError,
    MAX_SUBTASK_DEPTH,
)


def _forge(beads: Beads, **overrides) -> str:
    """Append a raw JSONL record, bypassing the write boundary. Returns its id.

    The write boundary now refuses ghost blockers and self-parents
    (TaskReferenceError), so the only ways such a record reaches disk are a
    hand edit, a bad merge, or data written before that guard existed. Those
    are exactly the cases the READ side must still tolerate, so the fixtures
    below forge them directly instead of going through create()/update().
    """
    record = {
        "id": f"ac-{uuid.uuid4().hex[:8]}",
        "title": "forged",
        "description": "",
        "status": "pending",
        "priority": 2,
        "blocked_by": [],
        "metadata": {},
    }
    record.update(overrides)
    with open(beads.path, "a") as f:
        f.write(json.dumps(record) + "\n")
    return record["id"]


def test_skipped_status_exists():
    assert TaskStatus.SKIPPED.value == "skipped"


def test_from_json_ignores_unknown_fields():
    line = json.dumps(
        {
            "id": "ac-1",
            "title": "t",
            "description": "d",
            "status": "pending",
            "priority": 2,
            "some_future_field": "ignored",
        }
    )
    task = Task.from_json(line)
    assert task.id == "ac-1"
    assert not hasattr(task, "some_future_field")


def test_read_all_quarantines_bad_lines(tmp_path, capsys):
    path = tmp_path / "tasks.jsonl"

    valid_a = Task(id="ac-a", title="A", description="da")
    valid_b = Task(id="ac-b", title="B", description="db")
    bogus_status = json.dumps(
        {"id": "ac-c", "title": "C", "description": "dc", "status": "bogus", "priority": 2}
    )
    garbage = "this is not json at all {{"

    path.write_text(
        valid_a.to_json() + "\n"
        + valid_b.to_json() + "\n"
        + bogus_status + "\n"
        + garbage + "\n"
    )

    beads = Beads(path)
    tasks = beads._read_all()

    assert len(tasks) == 2
    assert {t.id for t in tasks} == {"ac-a", "ac-b"}

    captured = capsys.readouterr()
    # Warnings go to stderr so they never corrupt --json stdout payloads.
    assert "quarantined" in captured.err.lower()
    # line numbers for the two bad lines (3 and 4)
    assert ":3" in captured.err
    assert ":4" in captured.err


def test_quarantined_lines_survive_update(tmp_path):
    path = tmp_path / "tasks.jsonl"

    valid_a = Task(id="ac-a", title="A", description="da")
    valid_b = Task(id="ac-b", title="B", description="db")
    bogus_status = json.dumps(
        {"id": "ac-c", "title": "C", "description": "dc", "status": "bogus", "priority": 2}
    )
    garbage = "this is not json at all {{"

    path.write_text(
        valid_a.to_json() + "\n"
        + valid_b.to_json() + "\n"
        + bogus_status + "\n"
        + garbage + "\n"
    )

    beads = Beads(path)
    updated = beads.update("ac-a", status=TaskStatus.DONE)
    assert updated.status == TaskStatus.DONE

    content = path.read_text()
    # bogus + garbage lines preserved verbatim
    assert bogus_status in content
    assert garbage in content
    # valid records still present
    assert "ac-a" in content
    assert "ac-b" in content


def test_ghost_blockers_returns_tasks_with_nonexistent_blockers(tmp_path):
    path = tmp_path / "tasks.jsonl"
    beads = Beads(path)

    real = beads.create(title="Real task", description="exists")
    # A well-formed id that names nothing — forged on disk, because create()
    # now refuses it (that refusal is tested in test_task_references.py). What
    # is under test here is that the READ side still reports it rather than
    # crashing on legacy data.
    ghost_id = "ac-deadbeef"
    stuck = _forge(beads, title="Stuck task", blocked_by=[ghost_id])
    also_stuck = _forge(beads, title="Doubly stuck", blocked_by=[ghost_id, real.id])
    clean = beads.create(title="Clean", description="no blockers")
    beads.complete(real.id)
    done_and_blocked = _forge(
        beads, title="Done but had ghost", blocked_by=[ghost_id], status="done"
    )

    results = beads.ghost_blockers()
    stuck_ids = {t.id for t, _ in results}

    assert stuck in stuck_ids
    assert also_stuck in stuck_ids
    assert clean.id not in stuck_ids
    assert done_and_blocked not in stuck_ids  # non-pending tasks excluded

    ghosts_for_stuck = next(g for t, g in results if t.id == stuck)
    assert ghosts_for_stuck == [ghost_id]


def test_ghost_blockers_empty_when_all_blockers_exist(tmp_path):
    path = tmp_path / "tasks.jsonl"
    beads = Beads(path)

    dep = beads.create(title="Dep", description="x")
    beads.create(title="Waiting", description="x", blocked_by=[dep.id])

    assert beads.ghost_blockers() == []


def test_create_refuses_to_exceed_max_subtask_depth(tmp_path):
    """The hard floor against runaway decomposition: a task cannot be created
    deeper than MAX_SUBTASK_DEPTH generations below a root."""
    # Derived from the constant, not hardcoded: the invariant under test is "the cap
    # is enforced", not "the cap is 2". Hardcoding it made this fail on a cap change
    # that did not break any behaviour.
    beads = Beads(tmp_path / "tasks.jsonl")

    node = beads.create(title="root", description="d0")  # depth 0
    assert beads._depth_of(node.id) == 0
    for gen in range(1, MAX_SUBTASK_DEPTH + 1):
        node = beads.create(title=f"d{gen}", description=f"d{gen}", parent_id=node.id)
        assert beads._depth_of(node.id) == gen

    # One generation past the cap is refused.
    with pytest.raises(DepthLimitError):
        beads.create(title="too deep", description="x", parent_id=node.id)


def test_depth_of_is_cycle_safe(tmp_path):
    """A self/loop parent reference must not hang the depth walk."""
    beads = Beads(tmp_path / "tasks.jsonl")
    # Forge a self-cycle directly on disk and confirm the walk terminates.
    # update() refuses a self-parent now, so this genuinely has to bypass it.
    t = _forge(beads, id="ac-aaaaaaaa", title="t", parent_id="ac-aaaaaaaa")
    assert beads._depth_of(t) >= 0  # returns, does not hang


def test_write_all_is_atomic_via_os_replace(tmp_path, monkeypatch):
    """Rewrites must go through os.replace (atomic rename), never an in-place
    truncating open('w') that a crash could leave half-written. Also confirms
    quarantined lines survive the atomic rewrite verbatim."""
    import os

    path = tmp_path / "tasks.jsonl"
    valid = Task(id="ac-a", title="A", description="da")
    garbage = "this is not json at all {{"
    path.write_text(valid.to_json() + "\n" + garbage + "\n")

    beads = Beads(path)

    calls: list[tuple] = []
    real_replace = os.replace

    def spy_replace(src, dst, *args, **kwargs):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", spy_replace)

    # update() reads (quarantining garbage) then rewrites via _write_all.
    beads.update("ac-a", status=TaskStatus.DONE)

    # The rewrite went through os.replace, renaming onto the live path.
    assert calls, "rewrite did not go through os.replace"
    assert any(dst == str(path) for _, dst in calls)
    # The temp source lived in the same directory (keeps rename atomic).
    assert all(os.path.dirname(src) == str(tmp_path) for src, _ in calls)

    # Quarantined line survived verbatim alongside the updated record.
    content = path.read_text()
    assert garbage in content
    assert '"ac-a"' in content
    assert '"done"' in content
    # No stray temp files left behind.
    assert not list(tmp_path.glob(".tasks-*.tmp"))


# ---------------- create: assigned_to + initial status on first append (HIGH-5, MEDIUM-7)


def test_create_carries_assigned_to_on_first_append(tmp_path):
    """A human assignment rides the FIRST JSONL line — no create→update window
    where the task is momentarily a plain agent PENDING task another process
    could grab."""
    path = tmp_path / "tasks.jsonl"
    beads = Beads(path)
    t = beads.create("desk work", "x", assigned_to="human:mabidoli")
    assert t.assigned_to == "human:mabidoli"

    # It is on disk as human-assigned from the very first record — the single
    # append already carried it (a fresh reader sees it, no second write needed).
    line = path.read_text().splitlines()[0]
    assert json.loads(line)["assigned_to"] == "human:mabidoli"
    # And such a task never enters the agent-dispatch ready set.
    assert t.id not in {r.id for r in beads.ready()}


def test_create_accepts_initial_status(tmp_path):
    beads = Beads(tmp_path / "tasks.jsonl")
    t = beads.create("proposal", "x", status=TaskStatus.PENDING_APPROVAL)
    assert t.status == TaskStatus.PENDING_APPROVAL
    # Born pending_approval → excluded from ready() from the first append.
    assert t.id not in {r.id for r in beads.ready()}
    assert Beads(tmp_path / "tasks.jsonl").get(t.id).status == TaskStatus.PENDING_APPROVAL


def test_create_defaults_are_unchanged(tmp_path):
    beads = Beads(tmp_path / "tasks.jsonl")
    t = beads.create("plain", "x")
    assert t.status == TaskStatus.PENDING
    assert t.assigned_to is None


def test_approve_clears_requires_approval_flag(tmp_path):
    """approve() promotes to PENDING and DROPS requires_approval — the flag is the
    gate, so a task past the gate must not still carry it (else the dispatch guard
    would quarantine a legitimately approved task)."""
    beads = Beads(tmp_path / "tasks.jsonl")
    t = beads.create(
        "proposed", "x", status=TaskStatus.PENDING_APPROVAL,
        metadata={"requires_approval": True, "planner_parent": "ac-parent"},
    )
    approved = beads.approve(t.id)
    assert approved.status == TaskStatus.PENDING
    assert "requires_approval" not in approved.metadata
    # Unrelated metadata is preserved.
    assert approved.metadata["planner_parent"] == "ac-parent"
