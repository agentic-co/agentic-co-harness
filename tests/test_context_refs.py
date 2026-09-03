"""Per-bead `metadata.context_refs` — plan-time file pointers, injected at
execution.

The contract's sharp edge is deliberate asymmetry: SHAPE is enforced at the
write boundary, EXISTENCE never is. A plan legitimately pins files a builder is
about to create, so a missing path warns twice (at write, and in the prompt)
and fails nowhere.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from agentco_harness.beads import (
    Beads,
    ContextRefsContractError,
    resolve_context_ref,
    validate_context_refs,
)
from agentco_harness.cli import main
from agentco_harness.executor import (
    CONTEXT_REF_FILE_CAP,
    CONTEXT_REF_TOTAL_CAP,
    _context_refs_block,
    run_store_backed_task,
)


def _node(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    return root / "config.yaml"


def _fake_claude(tmp_path: Path, script_body: str = "cat\n") -> str:
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n" + script_body)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return str(binary)


# --- shape validation -------------------------------------------------------


def test_a_well_formed_ref_list_is_accepted(tmp_path):
    refs = validate_context_refs(
        [{"path": "agentco/beads.py", "why": "the write boundary"}], tmp_path
    )
    assert refs == [{"path": "agentco/beads.py", "why": "the write boundary"}]


@pytest.mark.parametrize(
    "payload",
    [
        {"path": "x", "why": "y"},          # a bare object, not a list
        "agentco/beads.py",                  # a bare string
        7,
    ],
)
def test_non_list_payloads_are_refused(payload, tmp_path):
    with pytest.raises(ContextRefsContractError, match="must be a list"):
        validate_context_refs(payload, tmp_path)


def test_a_non_object_entry_is_refused_by_index(tmp_path):
    with pytest.raises(ContextRefsContractError, match=r"context_refs\[1\]"):
        validate_context_refs([{"path": "a", "why": "b"}, "c"], tmp_path)


def test_a_missing_or_blank_path_is_refused(tmp_path):
    with pytest.raises(ContextRefsContractError, match="'path'"):
        validate_context_refs([{"why": "no path"}], tmp_path)
    with pytest.raises(ContextRefsContractError, match="'path'"):
        validate_context_refs([{"path": "  ", "why": "blank"}], tmp_path)


def test_a_missing_why_is_refused(tmp_path):
    with pytest.raises(ContextRefsContractError, match="'why'"):
        validate_context_refs([{"path": "agentco/beads.py"}], tmp_path)


def test_unknown_keys_are_refused(tmp_path):
    with pytest.raises(ContextRefsContractError, match="unknown key"):
        validate_context_refs([{"path": "a", "why": "b", "lines": "1-20"}], tmp_path)


def test_a_missing_file_warns_but_is_stored(tmp_path, capsys):
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create(
        "build the thing",
        "d",
        metadata={"context_refs": [{"path": "not/written/yet.py", "why": "you create this"}]},
    )
    assert "does not exist yet" in capsys.readouterr().err
    assert beads.get(task.id).metadata["context_refs"][0]["path"] == "not/written/yet.py"


def test_an_existing_file_warns_about_nothing(tmp_path, capsys):
    (tmp_path / "here.py").write_text("x = 1\n")
    beads = Beads(tmp_path / "tasks.jsonl")
    beads.create("t", "d", metadata={"context_refs": [{"path": "here.py", "why": "reason"}]})
    assert "does not exist yet" not in capsys.readouterr().err


def test_refs_survive_an_update(tmp_path):
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create("t", "d")
    beads.update(task.id, metadata={"context_refs": [{"path": "a.py", "why": "b"}]})
    assert beads.get(task.id).metadata["context_refs"] == [{"path": "a.py", "why": "b"}]


def test_a_bad_update_payload_is_refused(tmp_path):
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create("t", "d")
    with pytest.raises(ContextRefsContractError):
        beads.update(task.id, metadata={"context_refs": [{"path": "a.py"}]})


def test_relative_resolves_against_the_node_absolute_stays_put(tmp_path):
    assert resolve_context_ref("sub/a.py", tmp_path) == tmp_path / "sub" / "a.py"
    assert resolve_context_ref("/etc/hosts", tmp_path) == Path("/etc/hosts")


# --- prompt injection -------------------------------------------------------


def test_the_referenced_file_is_injected_with_its_reason(tmp_path):
    node = tmp_path / "node"
    cfg = _node(node)
    (node / "target.py").write_text("def handler():\n    return 42\n")
    beads = Beads(node / "tasks.jsonl")
    task = beads.create(
        "patch the handler",
        "d",
        metadata={"context_refs": [{"path": "target.py", "why": "the function to patch"}]},
    )

    block = _context_refs_block(task.id, str(cfg))
    assert "BEAD CONTEXT" in block
    assert "## target.py — the function to patch" in block
    assert "def handler():" in block


def test_a_missing_file_is_noted_not_silently_skipped(tmp_path):
    node = tmp_path / "node"
    cfg = _node(node)
    beads = Beads(node / "tasks.jsonl")
    task = beads.create(
        "create it",
        "d",
        metadata={"context_refs": [{"path": "future.py", "why": "you will write this"}]},
    )

    block = _context_refs_block(task.id, str(cfg))
    assert "## future.py — you will write this" in block
    assert "not readable" in block
    assert "skipped" in block


def test_a_big_file_is_head_truncated_and_says_so(tmp_path):
    node = tmp_path / "node"
    cfg = _node(node)
    (node / "big.py").write_text("Z" * 10_000)
    beads = Beads(node / "tasks.jsonl")
    task = beads.create(
        "read it",
        "d",
        metadata={"context_refs": [{"path": "big.py", "why": "the big one"}]},
    )

    block = _context_refs_block(task.id, str(cfg))
    # "Z" appears nowhere in the surrounding prose, so this counts the excerpt.
    assert block.count("Z") == CONTEXT_REF_FILE_CAP
    assert f"showing the first {CONTEXT_REF_FILE_CAP} of 10000 chars" in block


def test_the_total_budget_is_enforced_across_files(tmp_path):
    node = tmp_path / "node"
    cfg = _node(node)
    refs = []
    for i in range(6):  # 6 × 2048 > the 8192 total cap
        (node / f"f{i}.py").write_text("Z" * 3000)
        refs.append({"path": f"f{i}.py", "why": f"file {i}"})
    beads = Beads(node / "tasks.jsonl")
    task = beads.create("read them", "d", metadata={"context_refs": refs})

    block = _context_refs_block(task.id, str(cfg))
    assert block.count("Z") <= CONTEXT_REF_TOTAL_CAP
    # Every ref is still ANNOUNCED — the agent learns the file exists and was
    # left out, rather than never hearing about it.
    for i in range(6):
        assert f"## f{i}.py — file {i}" in block
    assert "context budget" in block


def test_the_block_lands_in_the_store_backed_prompt(tmp_path):
    node = tmp_path / "node"
    cfg = _node(node)
    (node / "target.py").write_text("SENTINEL_CONTENT = 1\n")
    beads = Beads(node / "tasks.jsonl")
    task = beads.create(
        "patch it",
        "d",
        metadata={"context_refs": [{"path": "target.py", "why": "the file to patch"}]},
    )

    claude = _fake_claude(tmp_path)  # `cat` echoes the prompt back on stdout
    result = run_store_backed_task(
        task.id, config_path=str(cfg), claude_bin=claude, idle_timeout_s=0
    )
    assert "SENTINEL_CONTENT = 1" in result.output
    assert "the file to patch" in result.output
    # Ordering: bead context sits under PRIME and above the task instructions.
    assert result.output.index("BEAD CONTEXT") < result.output.index("You are executing")


def test_a_legacy_bead_gets_no_block_at_all(tmp_path):
    node = tmp_path / "node"
    cfg = _node(node)
    beads = Beads(node / "tasks.jsonl")
    task = beads.create("plain old bead", "d", metadata={"epic": "payments"})

    assert _context_refs_block(task.id, str(cfg)) == ""
    claude = _fake_claude(tmp_path)
    result = run_store_backed_task(
        task.id, config_path=str(cfg), claude_bin=claude, idle_timeout_s=0
    )
    assert "BEAD CONTEXT" not in result.output


def test_context_assembly_never_kills_a_run(tmp_path, monkeypatch):
    node = tmp_path / "node"
    cfg = _node(node)
    beads = Beads(node / "tasks.jsonl")
    task = beads.create("t", "d", metadata={"context_refs": [{"path": "a.py", "why": "b"}]})

    import agentco_harness.config as config_mod

    monkeypatch.setattr(
        config_mod.Config, "load", classmethod(lambda cls, p=None: (_ for _ in ()).throw(RuntimeError("boom")))
    )
    assert _context_refs_block(task.id, str(cfg)) == ""


def test_an_unknown_task_id_yields_no_block(tmp_path):
    cfg = _node(tmp_path / "node")
    assert _context_refs_block("ac-deadbeef", str(cfg)) == ""


# --- CLI rendering ----------------------------------------------------------


def test_tasks_show_renders_the_refs(tmp_path):
    node = tmp_path / "node"
    cfg = _node(node)
    (node / "here.py").write_text("x = 1\n")
    beads = Beads(node / "tasks.jsonl")
    task = beads.create(
        "t",
        "d",
        metadata={
            "context_refs": [
                {"path": "here.py", "why": "exists already"},
                {"path": "later.py", "why": "you will create this"},
            ]
        },
    )

    out = CliRunner().invoke(main, ["--config", str(cfg), "tasks", "show", task.id]).output
    assert "Context refs (pinned at plan time)" in out
    assert "why: exists already" in out
    assert "later.py  ⚠️ not on disk yet" in out


def test_tasks_show_json_flag_omits_the_refs_section(tmp_path):
    node = tmp_path / "node"
    cfg = _node(node)
    beads = Beads(node / "tasks.jsonl")
    task = beads.create("t", "d", metadata={"context_refs": [{"path": "a.py", "why": "b"}]})
    out = CliRunner().invoke(
        main, ["--config", str(cfg), "tasks", "show", task.id, "--json"]
    ).output
    assert "Context refs" not in out
