"""Per-bead `metadata.sop` — the delegation-ready block (ac-7ced1c85).

Five fields: purpose, trigger, inputs, definition_of_done, common_mistakes.
`definition_of_done` is the ISC in bead form; `common_mistakes` is the field
with no other home in LifeOS and the reason the block exists — every other
field describes the work, that one describes the failure modes, which is what a
handoff actually breaks on.

The contract's sharp edges: partial blocks are LEGAL (an SOP is filled in as
the work is understood), but the dishonest shapes are refused at the write
boundary — an empty block, a present-but-blank field, an empty mistakes list,
and more than three mistakes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from agentco_harness.beads import (
    MAX_SOP_MISTAKES,
    Beads,
    SopContractError,
    validate_sop,
)
from agentco_harness.cli import main


def _node(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text("tasks_path: tasks.jsonl\n")
    return root / "config.yaml"


FULL_SOP = {
    "purpose": "keep the weekly review honest",
    "trigger": "every Monday standup",
    "inputs": "last week's bead list",
    "definition_of_done": "every open human bead has a named owner",
    "common_mistakes": [
        "reviewing only your own beads",
        "closing a goal before its children",
    ],
}


# --- shape validation -------------------------------------------------------


def test_a_full_block_round_trips_through_the_validator():
    assert validate_sop(dict(FULL_SOP)) == FULL_SOP


def test_a_partial_block_is_legal():
    # The point: an SOP is filled in as the work is understood. Demanding all
    # five at create time is how the block gets skipped entirely.
    assert validate_sop({"definition_of_done": "the suite is green"}) == {
        "definition_of_done": "the suite is green"
    }


@pytest.mark.parametrize("payload", ["purpose: x", ["purpose"], 7, None])
def test_a_non_object_block_is_refused(payload):
    with pytest.raises(SopContractError, match="must be a JSON object"):
        validate_sop(payload)


def test_an_empty_block_is_refused():
    with pytest.raises(SopContractError, match="empty"):
        validate_sop({})


def test_unknown_keys_are_refused_and_steps_is_named():
    # `steps` is the field a reader of the source note will reach for first;
    # the error says where the steps actually live instead of just refusing.
    with pytest.raises(SopContractError, match="steps") as excinfo:
        validate_sop({"purpose": "p", "steps": "1. do it"})
    assert "description" in str(excinfo.value)


@pytest.mark.parametrize(
    "key", ["purpose", "trigger", "inputs", "definition_of_done"]
)
@pytest.mark.parametrize("value", ["", "   ", 7, None, ["a"]])
def test_a_present_but_blank_text_field_is_refused(key, value):
    with pytest.raises(SopContractError, match=f"'{key}'"):
        validate_sop({key: value})


# --- the mistakes list, and its cap -----------------------------------------


def test_three_mistakes_are_accepted():
    block = validate_sop({"common_mistakes": ["a", "b", "c"]})
    assert block["common_mistakes"] == ["a", "b", "c"]


def test_a_fourth_mistake_is_refused():
    with pytest.raises(SopContractError, match="the cap is 3"):
        validate_sop({"common_mistakes": ["a", "b", "c", "d"]})


def test_the_cap_constant_is_what_is_enforced():
    over = ["m"] * (MAX_SOP_MISTAKES + 1)
    with pytest.raises(SopContractError, match=f"carries {len(over)} entries"):
        validate_sop({"common_mistakes": over})


def test_a_bare_string_is_not_a_mistakes_list():
    # Refused rather than wrapped: silently accepting "abc" as one mistake also
    # silently accepts it as three characters to anything iterating.
    with pytest.raises(SopContractError, match="must be a LIST"):
        validate_sop({"common_mistakes": "forgot the migration"})


def test_an_empty_mistakes_list_is_refused():
    with pytest.raises(SopContractError, match="no known failure modes"):
        validate_sop({"common_mistakes": []})


def test_a_blank_mistake_entry_is_refused_by_index():
    with pytest.raises(SopContractError, match=r"common_mistakes'\]\[1\]"):
        validate_sop({"common_mistakes": ["real one", "  "]})


# --- persistence ------------------------------------------------------------


def test_the_block_round_trips_through_the_store(tmp_path):
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create("write the SOP", "d", metadata={"sop": dict(FULL_SOP)})
    # Re-read from disk, not the returned object: the JSONL is the record.
    assert beads.get(task.id).metadata["sop"] == FULL_SOP


def test_a_bad_block_is_refused_at_create_and_nothing_is_written(tmp_path):
    beads = Beads(tmp_path / "tasks.jsonl")
    with pytest.raises(SopContractError):
        beads.create("t", "d", metadata={"sop": {"common_mistakes": ["a", "b", "c", "d"]}})
    assert beads.list() == []


def test_the_block_survives_an_update(tmp_path):
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create("t", "d")
    beads.update(task.id, metadata={"sop": {"purpose": "added later"}})
    assert beads.get(task.id).metadata["sop"] == {"purpose": "added later"}


def test_a_bad_update_payload_is_refused(tmp_path):
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create("t", "d", metadata={"sop": {"purpose": "good"}})
    with pytest.raises(SopContractError):
        beads.update(task.id, metadata={"sop": {"purpose": ""}})
    assert beads.get(task.id).metadata["sop"] == {"purpose": "good"}


def test_a_legacy_bead_without_a_block_is_untouched(tmp_path):
    beads = Beads(tmp_path / "tasks.jsonl")
    task = beads.create("plain old bead", "d", metadata={"epic": "payments"})
    assert "sop" not in beads.get(task.id).metadata


# --- CLI create -------------------------------------------------------------


def test_create_with_every_sop_flag_stores_the_block(tmp_path):
    cfg = _node(tmp_path / "node")
    result = CliRunner().invoke(
        main,
        [
            "--config", str(cfg), "tasks", "create", "run the weekly review",
            "--sop-purpose", "keep the weekly review honest",
            "--sop-trigger", "every Monday standup",
            "--sop-inputs", "last week's bead list",
            "--dod", "every open human bead has a named owner",
            "--mistake", "reviewing only your own beads",
            "--mistake", "closing a goal before its children",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "sop: 5/5 field(s), 2 mistake(s)" in result.output

    task = Beads(tmp_path / "node" / "tasks.jsonl").list()[0]
    assert task.metadata["sop"] == FULL_SOP


def test_dod_alone_is_enough(tmp_path):
    cfg = _node(tmp_path / "node")
    result = CliRunner().invoke(
        main,
        ["--config", str(cfg), "tasks", "create", "t", "--dod", "the suite is green"],
    )
    assert result.exit_code == 0, result.output
    task = Beads(tmp_path / "node" / "tasks.jsonl").list()[0]
    assert task.metadata["sop"] == {"definition_of_done": "the suite is green"}


def test_create_without_sop_flags_writes_no_block(tmp_path):
    cfg = _node(tmp_path / "node")
    result = CliRunner().invoke(main, ["--config", str(cfg), "tasks", "create", "t"])
    assert result.exit_code == 0, result.output
    assert "sop:" not in result.output
    task = Beads(tmp_path / "node" / "tasks.jsonl").list()[0]
    assert "sop" not in task.metadata


def test_a_fourth_mistake_flag_refuses_and_creates_nothing(tmp_path):
    cfg = _node(tmp_path / "node")
    result = CliRunner().invoke(
        main,
        [
            "--config", str(cfg), "tasks", "create", "t",
            "--mistake", "a", "--mistake", "b", "--mistake", "c", "--mistake", "d",
        ],
    )
    assert result.exit_code == 1
    assert "the cap is 3" in result.output
    assert "Task NOT created" in result.output
    # The refusal is worth nothing if a bead landed anyway.
    assert Beads(tmp_path / "node" / "tasks.jsonl").list() == []


def test_a_blank_dod_refuses_and_creates_nothing(tmp_path):
    cfg = _node(tmp_path / "node")
    result = CliRunner().invoke(
        main, ["--config", str(cfg), "tasks", "create", "t", "--dod", "   "]
    )
    assert result.exit_code == 1
    assert "definition_of_done" in result.output
    assert Beads(tmp_path / "node" / "tasks.jsonl").list() == []


def test_the_sop_block_coexists_with_verify(tmp_path):
    # The two are complementary, not alternatives: --dod is done in prose for a
    # human, --verify is done as a command for the gate.
    cfg = _node(tmp_path / "node")
    result = CliRunner().invoke(
        main,
        [
            "--config", str(cfg), "tasks", "create", "t",
            "--dod", "the suite is green",
            "--verify", '{"class": "deterministic", "check": "uv run pytest -q"}',
        ],
    )
    assert result.exit_code == 0, result.output
    task = Beads(tmp_path / "node" / "tasks.jsonl").list()[0]
    assert task.metadata["sop"]["definition_of_done"] == "the suite is green"
    assert task.metadata["verify"]["check"] == "uv run pytest -q"


# --- CLI show ---------------------------------------------------------------


def test_show_renders_the_block(tmp_path):
    cfg = _node(tmp_path / "node")
    beads = Beads(tmp_path / "node" / "tasks.jsonl")
    task = beads.create("t", "d", metadata={"sop": dict(FULL_SOP)})

    out = CliRunner().invoke(main, ["--config", str(cfg), "tasks", "show", task.id]).output
    assert "SOP (delegation-ready block)" in out
    assert "purpose: keep the weekly review honest" in out
    assert "trigger: every Monday standup" in out
    assert "inputs: last week's bead list" in out
    assert "done when: every open human bead has a named owner" in out
    assert "- reviewing only your own beads" in out
    assert "- closing a goal before its children" in out


def test_show_names_missing_mistakes_rather_than_omitting_them(tmp_path):
    cfg = _node(tmp_path / "node")
    beads = Beads(tmp_path / "node" / "tasks.jsonl")
    task = beads.create("t", "d", metadata={"sop": {"purpose": "p"}})

    out = CliRunner().invoke(main, ["--config", str(cfg), "tasks", "show", task.id]).output
    assert "mistakes: (none recorded — add with --mistake)" in out


def test_show_omits_absent_fields(tmp_path):
    cfg = _node(tmp_path / "node")
    beads = Beads(tmp_path / "node" / "tasks.jsonl")
    task = beads.create("t", "d", metadata={"sop": {"purpose": "p"}})

    out = CliRunner().invoke(main, ["--config", str(cfg), "tasks", "show", task.id]).output
    assert "trigger:" not in out
    assert "done when:" not in out


def test_show_json_omits_the_section_but_keeps_the_payload(tmp_path):
    cfg = _node(tmp_path / "node")
    beads = Beads(tmp_path / "node" / "tasks.jsonl")
    task = beads.create("t", "d", metadata={"sop": dict(FULL_SOP)})

    out = CliRunner().invoke(
        main, ["--config", str(cfg), "tasks", "show", task.id, "--json"]
    ).output
    assert "SOP (delegation-ready block)" not in out
    # Still in the JSON — `--json` drops the rendering, never the data.
    import json as _json

    assert _json.loads(out)["metadata"]["sop"] == FULL_SOP


def test_show_on_a_legacy_bead_renders_no_sop_section(tmp_path):
    cfg = _node(tmp_path / "node")
    beads = Beads(tmp_path / "node" / "tasks.jsonl")
    task = beads.create("t", "d")

    out = CliRunner().invoke(main, ["--config", str(cfg), "tasks", "show", task.id]).output
    assert "SOP" not in out
