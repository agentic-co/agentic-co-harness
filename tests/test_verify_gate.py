"""The verify gate: metadata.verify contract, Beads.complete() enforcement,
awaiting_verify / verify_failed semantics, and blocker resolution.

Everything here runs against a tmp_path store with shell checks that touch
nothing outside tmp_path — no network, no real node.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from agentco_harness.beads import (
    Beads,
    TaskStatus,
    VerifyContractError,
    VerifyGateError,
    validate_verify,
)
from agentco_harness.cli import main
from agentco_harness.me import ranked


def _store(tmp_path: Path) -> Beads:
    return Beads(tmp_path / "tasks.jsonl")


def _config(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    cfg = root / "config.yaml"
    cfg.write_text("tasks_path: tasks.jsonl\n")
    return cfg


# --- contract validation ----------------------------------------------------


def test_validate_verify_normalizes_a_good_payload():
    spec = validate_verify(
        {"class": "deterministic", "check": "true", "cwd": "/tmp", "timeout_s": 5}
    )
    assert spec == {
        "class": "deterministic",
        "check": "true",
        "cwd": "/tmp",
        "timeout_s": 5,
    }


@pytest.mark.parametrize(
    "payload",
    [
        "not-a-dict",
        {"check": "true"},  # missing class
        {"class": "vibes", "check": "true"},  # unknown class
        {"class": "deterministic"},  # missing check
        {"class": "deterministic", "check": ""},  # empty check
        {"class": "deterministic", "check": "true", "timeout_s": 0},
        {"class": "deterministic", "check": "true", "timeout_s": "120"},
        {"class": "deterministic", "check": "true", "timeout": 120},  # typo'd key
        {"class": "deterministic", "check": "true", "cwd": ""},
    ],
)
def test_validate_verify_rejects_bad_shapes(payload):
    with pytest.raises(VerifyContractError):
        validate_verify(payload)


def test_create_rejects_invalid_verify_payload(tmp_path):
    beads = _store(tmp_path)
    with pytest.raises(VerifyContractError):
        beads.create("t", "d", metadata={"verify": {"class": "deterministic"}})
    # And nothing was written — the rejection is at the write boundary.
    assert beads.list() == []


def test_update_rejects_invalid_verify_payload(tmp_path):
    beads = _store(tmp_path)
    task = beads.create("t", "d")
    with pytest.raises(VerifyContractError):
        beads.update(task.id, metadata={"verify": {"class": "judged"}})
    assert beads.get(task.id).metadata == {}


# --- deterministic gate -----------------------------------------------------


def test_deterministic_pass_completes_and_records_evidence(tmp_path):
    beads = _store(tmp_path)
    task = beads.create(
        "ship it",
        "d",
        metadata={"verify": {"class": "deterministic", "check": "echo all-green"}},
    )
    done = beads.complete(task.id, result="agent says it works")

    assert done.status == TaskStatus.DONE
    record = done.metadata["verify_result"]
    assert record["passed"] is True
    assert record["exit_code"] == 0
    assert "all-green" in record["output_tail"]
    # The caller's own result is left untouched — it is often TaskResult JSON.
    assert done.result == "agent says it works"


def test_deterministic_failure_lands_in_verify_failed_with_output(tmp_path):
    beads = _store(tmp_path)
    task = beads.create(
        "ship it",
        "d",
        metadata={
            "verify": {
                "class": "deterministic",
                "check": "echo 'assertion blew up' >&2; exit 3",
            }
        },
    )
    out = beads.complete(task.id)

    assert out.status == TaskStatus.VERIFY_FAILED
    record = out.metadata["verify_result"]
    assert record["passed"] is False
    assert record["exit_code"] == 3
    assert "assertion blew up" in record["output_tail"]
    assert "assertion blew up" in out.result


def test_deterministic_failure_keeps_caller_result_intact(tmp_path):
    """A TaskResult JSON survives a failed gate — parsers downstream still work."""
    beads = _store(tmp_path)
    task = beads.create(
        "x", "d", metadata={"verify": {"class": "deterministic", "check": "exit 1"}}
    )
    payload = json.dumps({"status": "complete", "output": "did the thing"})
    out = beads.complete(task.id, result=payload)

    assert out.status == TaskStatus.VERIFY_FAILED
    assert json.loads(out.result)["output"] == "did the thing"


def test_deterministic_timeout_is_a_failure_not_a_pass(tmp_path):
    beads = _store(tmp_path)
    task = beads.create(
        "x",
        "d",
        metadata={
            "verify": {"class": "deterministic", "check": "sleep 5", "timeout_s": 1}
        },
    )
    out = beads.complete(task.id)

    assert out.status == TaskStatus.VERIFY_FAILED
    assert out.metadata["verify_result"]["timed_out"] is True
    assert out.metadata["verify_result"]["exit_code"] is None


def test_check_runs_in_the_store_directory_by_default(tmp_path):
    beads = _store(tmp_path)
    (tmp_path / "sentinel.txt").write_text("here")
    task = beads.create(
        "x", "d", metadata={"verify": {"class": "deterministic", "check": "test -f sentinel.txt"}}
    )
    assert beads.complete(task.id).status == TaskStatus.DONE


def test_explicit_cwd_wins(tmp_path):
    beads = _store(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "marker").write_text("x")
    task = beads.create(
        "x",
        "d",
        metadata={
            "verify": {
                "class": "deterministic",
                "check": "test -f marker",
                "cwd": str(elsewhere),
            }
        },
    )
    assert beads.complete(task.id).status == TaskStatus.DONE


def test_unrunnable_cwd_fails_the_gate(tmp_path):
    beads = _store(tmp_path)
    task = beads.create(
        "x",
        "d",
        metadata={
            "verify": {
                "class": "deterministic",
                "check": "true",
                "cwd": str(tmp_path / "does-not-exist"),
            }
        },
    )
    out = beads.complete(task.id)
    assert out.status == TaskStatus.VERIFY_FAILED
    assert "could not run check" in out.metadata["verify_result"]["output_tail"]


def test_generic_update_to_done_is_gated_too(tmp_path):
    """No side door: the gate sits on the status flip, not just complete()."""
    beads = _store(tmp_path)
    task = beads.create(
        "x", "d", metadata={"verify": {"class": "deterministic", "check": "exit 1"}}
    )
    out = beads.update(task.id, status=TaskStatus.DONE)
    assert out.status == TaskStatus.VERIFY_FAILED


def test_verify_failed_is_retryable_by_completing_again(tmp_path):
    beads = _store(tmp_path)
    flag = tmp_path / "fixed"
    task = beads.create(
        "x",
        "d",
        metadata={"verify": {"class": "deterministic", "check": "test -f fixed"}},
    )
    assert beads.complete(task.id).status == TaskStatus.VERIFY_FAILED

    flag.write_text("now it passes")
    assert beads.complete(task.id).status == TaskStatus.DONE


def test_verify_failed_can_be_sent_back_to_pending(tmp_path):
    """The other retry path: reopen for an agent, gate re-runs on next complete."""
    beads = _store(tmp_path)
    task = beads.create(
        "x", "d", metadata={"verify": {"class": "deterministic", "check": "exit 1"}}
    )
    beads.complete(task.id)
    reopened = beads.update(task.id, status=TaskStatus.PENDING, result=None)
    assert reopened.status == TaskStatus.PENDING
    assert beads.ready() and beads.ready()[0].id == task.id


# --- human gate -------------------------------------------------------------


def test_human_class_never_reaches_done_on_complete(tmp_path):
    beads = _store(tmp_path)
    task = beads.create(
        "email the customer",
        "d",
        metadata={"verify": {"class": "human", "check": "confirm the email was sent"}},
    )
    out = beads.complete(task.id, result="sent")
    assert out.status == TaskStatus.AWAITING_VERIFY
    assert out.metadata["verify_result"]["passed"] is None


def test_approve_verify_completes_and_records_approver(tmp_path):
    beads = _store(tmp_path)
    task = beads.create(
        "x", "d", metadata={"verify": {"class": "human", "check": "confirm"}}
    )
    beads.complete(task.id)
    approved = beads.approve_verify(task.id, approver="mabidoli")
    assert approved.status == TaskStatus.DONE
    assert approved.metadata["verify_approval"]["approver"] == "mabidoli"
    assert approved.metadata["verify_result"]["passed"] is True


def test_reject_verify_lands_in_verify_failed(tmp_path):
    beads = _store(tmp_path)
    task = beads.create(
        "x", "d", metadata={"verify": {"class": "human", "check": "confirm"}}
    )
    beads.complete(task.id)
    rejected = beads.reject_verify(task.id, approver="mabidoli", reason="wrong address")
    assert rejected.status == TaskStatus.VERIFY_FAILED
    assert "wrong address" in rejected.metadata["verify_rejection"]["reason"]


def test_approve_verify_refuses_a_bead_that_never_reached_the_gate(tmp_path):
    beads = _store(tmp_path)
    task = beads.create(
        "x", "d", metadata={"verify": {"class": "deterministic", "check": "exit 1"}}
    )
    beads.complete(task.id)  # -> verify_failed
    with pytest.raises(ValueError):
        beads.approve_verify(task.id, approver="mabidoli")


# --- judged -----------------------------------------------------------------


def test_judged_class_refuses_loudly(tmp_path):
    beads = _store(tmp_path)
    task = beads.create(
        "x", "d", metadata={"verify": {"class": "judged", "check": "is it good?"}}
    )
    with pytest.raises(VerifyGateError, match="not implemented in v1"):
        beads.complete(task.id)
    assert beads.get(task.id).status == TaskStatus.PENDING


# --- scope: legacy beads are untouched --------------------------------------


def test_legacy_bead_without_payload_keeps_exact_legacy_semantics(tmp_path):
    beads = _store(tmp_path)
    task = beads.create("legacy", "d")
    done = beads.complete(task.id, result="whatever the agent said")
    assert done.status == TaskStatus.DONE
    assert done.result == "whatever the agent said"
    assert "verify_result" not in done.metadata


def test_legacy_bead_with_unrelated_metadata_is_not_gated(tmp_path):
    beads = _store(tmp_path)
    task = beads.create("legacy", "d", metadata={"company": "m3bl", "epic": "launch"})
    assert beads.complete(task.id).status == TaskStatus.DONE


# --- scope: self-reporting executors cannot self-certify DONE (ac-fcc95ca5) --
#
# Live incident, 2026-08-18: four beads filed `--task-class agent -a claude`;
# the hourly cycle marked three DONE with no evidence the claimed work
# happened. One spot-checked and proved false. These tests pin the two
# responses `_classify_specless_done` produces for a spec-less DONE.


def test_agent_executed_bead_without_verify_is_tagged_unverified_not_silent(tmp_path):
    beads = _store(tmp_path)
    task = beads.create("do the thing", "d", assigned_agent="claude")
    done = beads.complete(task.id, result="I did it, trust me")
    assert done.status == TaskStatus.DONE
    assert done.metadata["verify_result"]["class"] == "unverified"
    assert done.metadata["verify_result"]["passed"] is None


def test_zai_executed_bead_without_verify_is_also_tagged(tmp_path):
    beads = _store(tmp_path)
    task = beads.create("do the thing", "d", assigned_agent="zai")
    done = beads.complete(task.id)
    assert done.status == TaskStatus.DONE
    assert done.metadata["verify_result"]["class"] == "unverified"


def test_metadata_executor_field_is_also_checked(tmp_path):
    """A caller can route dispatch through metadata.executor instead of
    assigned_agent (orchestrator._execute_cycle_task checks both) — the
    classifier must too."""
    beads = _store(tmp_path)
    task = beads.create("do the thing", "d", metadata={"executor": "forge"})
    done = beads.complete(task.id)
    assert done.status == TaskStatus.DONE
    assert done.metadata["verify_result"]["class"] == "unverified"


def test_agent_class_bead_without_verify_reaches_no_done_record(tmp_path):
    """The exact shape of the live incident: --task-class agent -a claude,
    no --verify. This is the hard block — the bead does NOT reach DONE on
    the executor's word alone."""
    beads = _store(tmp_path)
    task = beads.create(
        "[Leeloo owes] fix the thing",
        "d",
        assigned_agent="claude",
        metadata={"task_class": "agent"},
    )
    result = beads.complete(task.id, result="done, I promise")
    assert result.status == TaskStatus.AWAITING_VERIFY
    assert result.status != TaskStatus.DONE
    assert result.metadata["verify_result"]["passed"] is None
    # And a fresh read confirms nothing DONE ever hit disk for this bead.
    assert beads.get(task.id).status == TaskStatus.AWAITING_VERIFY


def test_agent_class_bead_with_passing_verify_spec_still_completes(tmp_path):
    """task_class == 'agent' does not override a REAL verify spec — a
    declared deterministic gate still runs and still governs."""
    beads = _store(tmp_path)
    task = beads.create(
        "fix it",
        "d",
        assigned_agent="claude",
        metadata={"task_class": "agent", "verify": {"class": "deterministic", "check": "true"}},
    )
    done = beads.complete(task.id)
    assert done.status == TaskStatus.DONE
    assert done.metadata["verify_result"]["class"] == "deterministic"
    assert done.metadata["verify_result"]["passed"] is True


def test_agent_class_bead_with_failing_verify_spec_is_verify_failed(tmp_path):
    beads = _store(tmp_path)
    task = beads.create(
        "fix it",
        "d",
        assigned_agent="claude",
        metadata={"task_class": "agent", "verify": {"class": "deterministic", "check": "false"}},
    )
    result = beads.complete(task.id)
    assert result.status == TaskStatus.VERIFY_FAILED


def test_recurring_spawned_agent_bead_is_exempt_from_the_unverified_tag(tmp_path):
    """The system's own routine work (recurring-spawned samples) is not
    flooded with unverified tags — same discriminator recurring.py's
    _sampler_family already uses."""
    beads = _store(tmp_path)
    task = beads.create(
        "nightly digest",
        "d",
        assigned_agent="zai",
        source="recurring",
    )
    done = beads.complete(task.id)
    assert done.status == TaskStatus.DONE
    assert "verify_result" not in done.metadata


def test_spawned_by_agent_bead_is_exempt_from_the_unverified_tag(tmp_path):
    beads = _store(tmp_path)
    task = beads.create(
        "sample",
        "d",
        assigned_agent="claude",
        metadata={"spawned_by": "verify-feeds"},
    )
    done = beads.complete(task.id)
    assert done.status == TaskStatus.DONE
    assert "verify_result" not in done.metadata


def test_rca_source_agent_class_bead_reaches_done_tagged_unverified(tmp_path):
    """ac-fcc95ca5 blast-radius fix: an RCA-generated bead now defaults to
    task_class=agent too (the `-a` default), but RCA beads have their own
    review flow and are not self-reported open-ended work — they must NOT
    hard-block to AWAITING_VERIFY. They still land tagged 'unverified' (not
    'none') so they stay spot-checkable, same as any other routine bead."""
    beads = _store(tmp_path)
    task = beads.create(
        "[RCA] Verify child instance: something",
        "d",
        assigned_agent="claude",
        source="rca",
        metadata={"task_class": "agent"},
    )
    done = beads.complete(task.id, result="verified")
    assert done.status == TaskStatus.DONE
    assert done.metadata["verify_result"]["class"] == "unverified"
    assert done.metadata["verify_result"]["passed"] is None


def test_manual_source_agent_class_bead_still_hard_blocks(tmp_path):
    """Control for the RCA exemption above: a genuine manually-filed
    agent-class bead (source == 'manual', the real shape of open-ended
    self-reported work) is untouched by the rca carve-out and still routes
    to AWAITING_VERIFY."""
    beads = _store(tmp_path)
    task = beads.create(
        "[Leeloo owes] fix the thing",
        "d",
        assigned_agent="claude",
        source="manual",
        metadata={"task_class": "agent"},
    )
    result = beads.complete(task.id, result="done, I promise")
    assert result.status == TaskStatus.AWAITING_VERIFY
    assert result.status != TaskStatus.DONE


def test_deterministic_in_process_dispatch_is_not_gated(tmp_path):
    """verify_child/retro claim under their OWN name (orchestrator's
    _execute_verify_child / _execute_retro), never one of
    SELF_REPORTING_EXECUTORS — a code-computed completion, not a self-report,
    so it must never pick up the unverified tag."""
    beads = _store(tmp_path)
    task = beads.create("verify child: feeds", "d")
    beads.claim(task.id, "verify_child")
    done = beads.complete(task.id, result='{"level": "ok"}')
    assert done.status == TaskStatus.DONE
    assert "verify_result" not in done.metadata


def test_report_result_path_is_gated_the_same_way(tmp_path):
    """report_result routes through update() too (the lease/remote-worker
    path) — the gate must apply there exactly as it does to complete()."""
    beads = _store(tmp_path)
    task = beads.create("fix it", "d", metadata={"task_class": "agent"})
    beads.claim(task.id, "claude")
    claimed = beads.get(task.id)
    result = beads.report_result(
        task.id, attempt=claimed.lease_attempt, status=TaskStatus.DONE, result="done"
    )
    assert result.status == TaskStatus.AWAITING_VERIFY


def test_human_assigned_bead_is_never_tagged_unverified(tmp_path):
    """assigned_to human: is a person's own claim of completion, not an
    executor self-report — out of scope for this gate entirely."""
    beads = _store(tmp_path)
    task = beads.create("call the vendor", "d", assigned_to="human:mabidoli")
    done = beads.update(task.id, status=TaskStatus.DONE, allow_human_reassign=False)
    assert done.status == TaskStatus.DONE
    assert "verify_result" not in done.metadata


# --- blocker resolution (the TOCTOU kill) -----------------------------------


def test_downstream_stays_blocked_through_awaiting_verify_and_verify_failed(tmp_path):
    beads = _store(tmp_path)
    blocker = beads.create(
        "gated blocker",
        "d",
        metadata={"verify": {"class": "human", "check": "confirm"}},
    )
    downstream = beads.create("depends on it", "d", blocked_by=[blocker.id])

    assert beads.ready() == [] or downstream.id not in {t.id for t in beads.ready()}

    beads.complete(blocker.id)
    assert beads.get(blocker.id).status == TaskStatus.AWAITING_VERIFY
    assert downstream.id not in {t.id for t in beads.ready()}

    beads.reject_verify(blocker.id, approver="mabidoli", reason="no")
    assert downstream.id not in {t.id for t in beads.ready()}

    # Only a genuinely passed gate releases the chain.
    beads.update(blocker.id, status=TaskStatus.AWAITING_VERIFY, verify_gate=False)
    beads.approve_verify(blocker.id, approver="mabidoli")
    assert downstream.id in {t.id for t in beads.ready()}


def test_deterministic_failure_does_not_release_downstream(tmp_path):
    beads = _store(tmp_path)
    blocker = beads.create(
        "gated", "d", metadata={"verify": {"class": "deterministic", "check": "exit 1"}}
    )
    downstream = beads.create("after", "d", blocked_by=[blocker.id])
    beads.complete(blocker.id)
    assert downstream.id not in {t.id for t in beads.ready()}


# --- me surfacing -----------------------------------------------------------


def test_me_surfaces_verify_gate_and_verify_failed(tmp_path):
    root = tmp_path / "node"
    cfg = _config(root)
    beads = Beads(root / "tasks.jsonl")

    gated = beads.create(
        "send the invoice",
        "d",
        metadata={"verify": {"class": "human", "check": "confirm invoice sent"}},
    )
    beads.complete(gated.id)
    failed = beads.create(
        "run migrations",
        "d",
        metadata={"verify": {"class": "deterministic", "check": "exit 2"}},
    )
    beads.complete(failed.id)

    items = {i.task_id: i for i in ranked(str(cfg))}
    assert items[gated.id].kind == "verify_gate"
    assert "approve-verify" in items[gated.id].resolve
    assert items[failed.id].kind == "verify_failed"
    # A failed gate outranks a pending one: it is evidenced, not merely waiting.
    assert items[failed.id].score > items[gated.id].score


def test_me_prefers_verify_gate_over_human_assigned_for_gated_beads(tmp_path):
    root = tmp_path / "node"
    cfg = _config(root)
    root.joinpath("config.yaml").write_text(
        "tasks_path: tasks.jsonl\nhumans:\n  enabled: true\n"
    )
    beads = Beads(root / "tasks.jsonl")
    task = beads.create(
        "call the supplier",
        "d",
        assigned_to="human:mabidoli",
        metadata={"verify": {"class": "human", "check": "confirm the call happened"}},
    )
    beads.complete(task.id)

    item = next(i for i in ranked(str(cfg)) if i.task_id == task.id)
    assert item.kind == "verify_gate"


# --- CLI --------------------------------------------------------------------


def test_cli_complete_exits_nonzero_on_a_failed_gate(tmp_path, monkeypatch):
    runner = CliRunner()
    root = tmp_path / "node"
    _config(root)
    monkeypatch.chdir(root)
    beads = Beads(root / "tasks.jsonl")
    task = beads.create(
        "x",
        "d",
        metadata={"verify": {"class": "deterministic", "check": "echo boom; exit 1"}},
    )

    result = runner.invoke(main, ["tasks", "complete", task.id])
    assert result.exit_code == 1, result.output
    assert "VERIFY FAILED" in result.output
    assert beads.get(task.id).status == TaskStatus.VERIFY_FAILED


def test_cli_approve_and_reject_verify_roundtrip(tmp_path, monkeypatch):
    runner = CliRunner()
    root = tmp_path / "node"
    _config(root)
    monkeypatch.chdir(root)
    beads = Beads(root / "tasks.jsonl")
    task = beads.create(
        "x", "d", metadata={"verify": {"class": "human", "check": "confirm"}}
    )

    result = runner.invoke(main, ["tasks", "complete", task.id])
    assert result.exit_code == 0, result.output
    assert "Awaiting verification" in result.output

    result = runner.invoke(
        main, ["tasks", "reject-verify", task.id, "-m", "not good enough"]
    )
    assert result.exit_code == 0, result.output
    assert beads.get(task.id).status == TaskStatus.VERIFY_FAILED

    # Approving something that is not at the gate must refuse.
    result = runner.invoke(main, ["tasks", "approve-verify", task.id])
    assert result.exit_code == 1
    assert "Cannot approve" in result.output

    beads.update(task.id, status=TaskStatus.AWAITING_VERIFY, verify_gate=False)
    result = runner.invoke(
        main, ["tasks", "approve-verify", task.id, "--approver", "mabidoli"]
    )
    assert result.exit_code == 0, result.output
    assert beads.get(task.id).status == TaskStatus.DONE


def test_cli_list_accepts_the_new_statuses(tmp_path, monkeypatch):
    runner = CliRunner()
    root = tmp_path / "node"
    _config(root)
    monkeypatch.chdir(root)
    beads = Beads(root / "tasks.jsonl")
    task = beads.create(
        "x", "d", metadata={"verify": {"class": "human", "check": "confirm"}}
    )
    beads.complete(task.id)

    result = runner.invoke(main, ["tasks", "list", "--status", "awaiting_verify"])
    assert result.exit_code == 0, result.output
    assert task.id in result.output


# --- --verify-check: the cheap declaration path ------------------------------


def test_cli_verify_check_is_shorthand_for_deterministic_json(tmp_path, monkeypatch):
    runner = CliRunner()
    root = tmp_path / "node"
    _config(root)
    monkeypatch.chdir(root)
    beads = Beads(root / "tasks.jsonl")

    result = runner.invoke(
        main, ["tasks", "create", "fix it", "--verify-check", "uv run pytest -q"]
    )
    assert result.exit_code == 0, result.output
    tasks = beads.list()
    assert len(tasks) == 1
    assert tasks[0].metadata["verify"] == {
        "class": "deterministic",
        "check": "uv run pytest -q",
    }


def test_cli_verify_and_verify_check_are_mutually_exclusive(tmp_path, monkeypatch):
    runner = CliRunner()
    root = tmp_path / "node"
    _config(root)
    monkeypatch.chdir(root)
    beads = Beads(root / "tasks.jsonl")

    result = runner.invoke(
        main,
        [
            "tasks",
            "create",
            "fix it",
            "--verify",
            '{"class": "deterministic", "check": "true"}',
            "--verify-check",
            "true",
        ],
    )
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output
    assert beads.list() == []
