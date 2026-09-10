"""The guard that would have caught the 2026-08-17 vault loss on the day.

Each test is the failure case, not the success case: the operation always
"succeeds" from the shell's point of view, and the question is whether the
guard notices what it took with it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from manifest_guard import lost, manifest, run  # noqa: E402


def _vault(tmp_path: Path, names: list[str]) -> Path:
    root = tmp_path / "vault"
    (root / "nested").mkdir(parents=True)
    for name in names:
        (root / name).write_text(f"# {name}\n")
    return root


def test_a_successful_command_that_deletes_a_note_is_refused(tmp_path):
    """The 2026-08-17 shape: the restructure exited 0 and six notes were gone."""
    root = _vault(tmp_path, ["keep.md", "doomed.md"])
    code = run([
        "--root", str(root), "--glob", "*.md", "--",
        "sh", "-c", f"rm {root / 'doomed.md'}",
    ])
    assert code == 1


def test_a_move_that_loses_nothing_passes(tmp_path):
    root = _vault(tmp_path, ["a.md", "b.md"])
    code = run([
        "--root", str(root), "--glob", "*.md", "--",
        "sh", "-c", f"mv {root / 'a.md'} {root / 'nested' / 'a.md'}",
    ])
    assert code == 0
    assert set(manifest(root, "*.md")) == {"b.md", "nested/a.md"}


def test_an_intended_deletion_can_be_declared_in_advance(tmp_path):
    root = _vault(tmp_path, ["a.md", "stale.md"])
    code = run([
        "--root", str(root), "--glob", "*.md", "--expect-deleted", "1", "--",
        "sh", "-c", f"rm {root / 'stale.md'}",
    ])
    assert code == 0


def test_more_deletions_than_declared_is_still_refused(tmp_path):
    """Declaring one does not license two — the margin is the point."""
    root = _vault(tmp_path, ["a.md", "b.md", "c.md"])
    code = run([
        "--root", str(root), "--glob", "*.md", "--expect-deleted", "1", "--",
        "sh", "-c", f"rm {root / 'b.md'} {root / 'c.md'}",
    ])
    assert code == 1


def test_a_failing_command_that_lost_nothing_reports_its_own_status(tmp_path):
    """Distinguish 'the operation broke' from 'the operation ate something'.
    They need different responses and must not be the same exit code."""
    root = _vault(tmp_path, ["a.md"])
    code = run(["--root", str(root), "--glob", "*.md", "--", "sh", "-c", "exit 7"])
    assert code == 7


def test_the_manifest_sees_nested_files(tmp_path):
    root = _vault(tmp_path, ["a.md"])
    (root / "nested" / "deep.md").write_text("x")
    assert set(manifest(root, "*.md")) == {"a.md", "nested/deep.md"}


def test_a_mass_move_is_not_a_loss(tmp_path):
    """The case that made content the identity: a restructure moves everything
    and loses nothing. A path-keyed guard would call this 3 deletions, get
    switched off for crying wolf, and be absent the day something real goes."""
    root = _vault(tmp_path, ["a.md", "b.md", "c.md"])
    code = run([
        "--root", str(root), "--glob", "*.md", "--",
        "sh", "-c", f"mv {root}/*.md {root / 'nested'}/",
    ])
    assert code == 0


def test_losing_one_of_two_identical_notes_is_still_a_loss(tmp_path):
    """Why a Counter and not a set: duplicates are legitimate, and losing one
    of a pair still loses one."""
    root = tmp_path / "vault"
    (root / "nested").mkdir(parents=True)
    (root / "one.md").write_text("same bytes\n")
    (root / "two.md").write_text("same bytes\n")
    before = manifest(root, "*.md")
    (root / "two.md").unlink()
    assert lost(before, manifest(root, "*.md")) == ["two.md"]
