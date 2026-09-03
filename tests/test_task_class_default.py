"""`agentco tasks create -a/--agent <x>` defaults --task-class to "agent"
(ac-fcc95ca5 follow-up).

The verify gate's hard block (test_verify_gate.py) only fires when
metadata.task_class == "agent" — and until this fix, nothing set that at
filing time, so the block depended on someone remembering to type
`--task-class agent`. This defaults it whenever `-a/--agent` is passed,
while an explicit `--task-class` always wins.

Blast radius (measured 2026-08-19, see the mabidoli request this closes):
of beads filed with an assigned_agent, 233 were `source: manual` ritual
beads from ~/Portfolio/rituals/run.sh and 55 were `source: rca` — neither
is genuine open-ended self-reported agent work. Both get a real exemption:
rituals now file an explicit `--task-class ritual` (run.sh, edited in
place — not covered by these tests, which are scoped to this repo), and
`source == "rca"` is exempted from the hard block in beads.py (see
test_verify_gate.py::test_rca_source_agent_class_bead_reaches_done_tagged_unverified).
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from agentco_harness.beads import Beads
from agentco_harness.cli import main


def _node(tmp_path: Path, monkeypatch) -> Beads:
    root = tmp_path / "node"
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    monkeypatch.chdir(root)
    return Beads(root / "tasks.jsonl")


def test_agent_flag_alone_defaults_task_class_to_agent(tmp_path, monkeypatch):
    beads = _node(tmp_path, monkeypatch)
    result = CliRunner().invoke(main, ["tasks", "create", "fix the thing", "-a", "claude"])
    assert result.exit_code == 0, result.output

    created = [t for t in beads.list() if t.title == "fix the thing"][0]
    assert created.metadata["task_class"] == "agent"


def test_explicit_task_class_overrides_the_agent_default(tmp_path, monkeypatch):
    beads = _node(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        main,
        ["tasks", "create", "quarterly sync", "-a", "claude", "--task-class", "company"],
    )
    assert result.exit_code == 0, result.output

    created = [t for t in beads.list() if t.title == "quarterly sync"][0]
    assert created.metadata["task_class"] == "company"


def test_no_agent_flag_leaves_task_class_unset(tmp_path, monkeypatch):
    beads = _node(tmp_path, monkeypatch)
    result = CliRunner().invoke(main, ["tasks", "create", "unassigned work"])
    assert result.exit_code == 0, result.output

    created = [t for t in beads.list() if t.title == "unassigned work"][0]
    assert "task_class" not in (created.metadata or {})


def test_ritual_task_class_is_a_valid_explicit_choice(tmp_path, monkeypatch):
    """The enum gained a `ritual` member so ~/Portfolio/rituals/run.sh can
    declare what it is instead of falling into the bare `-a` default."""
    beads = _node(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        main,
        ["tasks", "create", "StandUp 2026-08-19", "-a", "claude", "--task-class", "ritual"],
    )
    assert result.exit_code == 0, result.output

    created = [t for t in beads.list() if t.title == "StandUp 2026-08-19"][0]
    assert created.metadata["task_class"] == "ritual"
