"""Staged deterministic verify: `metadata.verify.checks` as an ordered ladder.

The staging claim under test is not "the gate fails" — it is "the gate stops".
Every failure case here proves the stages AFTER the failing one never ran, via
a sentinel file a later stage would have created.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from agentco_harness.beads import (
    Beads,
    TaskStatus,
    VerifyContractError,
    validate_verify,
    verify_check_text,
)
from agentco_harness.cli import main


def _store(tmp_path: Path) -> Beads:
    return Beads(tmp_path / "tasks.jsonl")


def _config(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    cfg = root / "config.yaml"
    cfg.write_text("tasks_path: tasks.jsonl\n")
    return cfg


# --- contract validation ----------------------------------------------------


def test_checks_list_is_accepted_and_normalized():
    spec = validate_verify(
        {"class": "deterministic", "checks": ["ruff check .", "uv run pytest -q"]}
    )
    assert spec["kind"] == "deterministic"
    assert spec["checks"] == ["ruff check .", "uv run pytest -q"]
    assert spec["check"] is None


def test_check_and_checks_together_are_refused():
    with pytest.raises(VerifyContractError, match="mutually exclusive"):
        validate_verify({"class": "deterministic", "check": "a", "checks": ["b"]})


def test_neither_check_nor_checks_is_refused():
    with pytest.raises(VerifyContractError, match="neither 'check'"):
        validate_verify({"class": "deterministic"})


def test_empty_checks_list_is_refused():
    with pytest.raises(VerifyContractError, match="non-empty list"):
        validate_verify({"class": "deterministic", "checks": []})


def test_blank_stage_is_refused_by_index():
    with pytest.raises(VerifyContractError, match=r"'checks'\[1\]"):
        validate_verify({"class": "deterministic", "checks": ["true", "  "]})


def test_non_string_stage_is_refused():
    with pytest.raises(VerifyContractError, match=r"'checks'\[0\]"):
        validate_verify({"class": "deterministic", "checks": [7]})


def test_a_bare_string_is_not_mistaken_for_a_one_stage_ladder():
    with pytest.raises(VerifyContractError, match="must be a LIST"):
        validate_verify({"class": "deterministic", "checks": "uv run pytest"})


def test_cwd_and_timeout_still_apply_to_a_staged_payload(tmp_path):
    spec = validate_verify(
        {
            "class": "deterministic",
            "checks": ["true"],
            "cwd": str(tmp_path),
            "timeout_s": 30,
        }
    )
    assert spec["cwd"] == str(tmp_path) and spec["timeout_s"] == 30


def test_check_text_flattens_both_shapes():
    assert verify_check_text({"check": "one"}) == "one"
    assert verify_check_text({"checks": ["a", "b"]}) == "a → b"
    assert verify_check_text(None) == ""


# --- gate execution ---------------------------------------------------------


def test_all_stages_pass_means_done(tmp_path):
    beads = _store(tmp_path)
    task = beads.create(
        "staged work",
        "d",
        metadata={
            "class": "x",
            "verify": {
                "class": "deterministic",
                "checks": ["true", "true", "true"],
                "cwd": str(tmp_path),
            },
        },
    )
    done = beads.complete(task.id)
    assert done.status == TaskStatus.DONE
    record = done.metadata["verify_result"]
    assert record["passed"] is True
    assert record["stages_run"] == 3 and record["stages_total"] == 3
    assert "failed_stage" not in record


def test_failure_at_stage_two_names_the_stage_and_stops_the_ladder(tmp_path):
    beads = _store(tmp_path)
    sentinel = tmp_path / "stage3-ran"
    task = beads.create(
        "staged work",
        "d",
        metadata={
            "verify": {
                "class": "deterministic",
                "checks": [
                    "true",
                    "echo 'mypy: 3 errors' >&2; exit 1",
                    f"touch {sentinel}",
                ],
                "cwd": str(tmp_path),
            }
        },
    )
    gated = beads.complete(task.id)

    assert gated.status == TaskStatus.VERIFY_FAILED
    record = gated.metadata["verify_result"]
    assert record["passed"] is False
    assert record["stages_run"] == 2 and record["stages_total"] == 3
    stage = record["failed_stage"]
    assert stage["index"] == 1
    assert stage["command"] == "echo 'mypy: 3 errors' >&2; exit 1"
    assert "mypy: 3 errors" in stage["output_tail"]
    # The claim that matters: stage 3 was never run.
    assert not sentinel.exists()


def test_first_stage_failure_runs_nothing_else(tmp_path):
    beads = _store(tmp_path)
    sentinel = tmp_path / "later-ran"
    task = beads.create(
        "staged work",
        "d",
        metadata={
            "verify": {
                "class": "deterministic",
                "checks": ["exit 3", f"touch {sentinel}"],
                "cwd": str(tmp_path),
            }
        },
    )
    gated = beads.complete(task.id)
    assert gated.status == TaskStatus.VERIFY_FAILED
    assert gated.metadata["verify_result"]["failed_stage"]["index"] == 0
    assert gated.metadata["verify_result"]["exit_code"] == 3
    assert not sentinel.exists()


def test_staged_failure_result_text_names_the_failing_command(tmp_path):
    beads = _store(tmp_path)
    task = beads.create(
        "staged work",
        "d",
        metadata={
            "verify": {
                "class": "deterministic",
                "checks": ["true", "exit 1"],
                "cwd": str(tmp_path),
            }
        },
    )
    gated = beads.complete(task.id)
    assert "verify failed: exit 1" in (gated.result or "")


def test_retrying_a_staged_gate_reruns_from_stage_one(tmp_path):
    beads = _store(tmp_path)
    gate = tmp_path / "gate-open"
    task = beads.create(
        "staged work",
        "d",
        metadata={
            "verify": {
                "class": "deterministic",
                "checks": ["true", f"test -f {gate}"],
                "cwd": str(tmp_path),
            }
        },
    )
    assert beads.complete(task.id).status == TaskStatus.VERIFY_FAILED
    gate.write_text("open")
    assert beads.complete(task.id).status == TaskStatus.DONE


def test_single_check_record_shape_is_unchanged(tmp_path):
    beads = _store(tmp_path)
    task = beads.create(
        "plain work",
        "d",
        metadata={"verify": {"class": "deterministic", "check": "true", "cwd": str(tmp_path)}},
    )
    done = beads.complete(task.id)
    record = done.metadata["verify_result"]
    assert done.status == TaskStatus.DONE
    assert record["check"] == "true" and record["passed"] is True
    assert "checks" not in record and "failed_stage" not in record
    assert "stages_total" not in record


def test_a_staged_bead_blocks_downstream_until_every_stage_passes(tmp_path):
    beads = _store(tmp_path)
    gate = tmp_path / "ok"
    blocker = beads.create(
        "staged",
        "d",
        metadata={
            "verify": {
                "class": "deterministic",
                "checks": ["true", f"test -f {gate}"],
                "cwd": str(tmp_path),
            }
        },
    )
    downstream = beads.create("after", "d", blocked_by=[blocker.id])
    beads.complete(blocker.id)
    assert downstream.id not in {t.id for t in beads.ready()}
    gate.write_text("x")
    beads.complete(blocker.id)
    assert downstream.id in {t.id for t in beads.ready()}


def test_human_class_with_staged_checks_records_readable_text(tmp_path):
    beads = _store(tmp_path)
    task = beads.create(
        "outward action",
        "d",
        metadata={"verify": {"class": "human", "checks": ["confirm email sent", "confirm reply"]}},
    )
    parked = beads.complete(task.id)
    assert parked.status == TaskStatus.AWAITING_VERIFY
    assert parked.metadata["verify_result"]["check"] == "confirm email sent → confirm reply"


# --- CLI rendering ----------------------------------------------------------


def test_tasks_show_renders_the_ladder_and_the_failing_stage(tmp_path):
    node = tmp_path / "node"
    cfg = _config(node)
    beads = Beads(node / "tasks.jsonl")
    sentinel = node / "never"
    task = beads.create(
        "staged work",
        "d",
        metadata={
            "verify": {
                "class": "deterministic",
                "checks": ["true", "exit 1", f"touch {sentinel}"],
                "cwd": str(node),
            }
        },
    )
    beads.complete(task.id)

    out = CliRunner().invoke(main, ["--config", str(cfg), "tasks", "show", task.id]).output
    assert "3 stage(s), stop at first failure" in out
    assert "1. true  ✅" in out
    assert "2. exit 1  ❌" in out
    assert "not run" in out
    assert "failed at stage 2/3" in out


def test_tasks_show_json_flag_still_emits_only_json(tmp_path):
    node = tmp_path / "node"
    cfg = _config(node)
    beads = Beads(node / "tasks.jsonl")
    task = beads.create(
        "staged",
        "d",
        metadata={"verify": {"class": "deterministic", "checks": ["true"]}},
    )
    out = CliRunner().invoke(
        main, ["--config", str(cfg), "tasks", "show", task.id, "--json"]
    ).output
    assert "stage(s)" not in out
