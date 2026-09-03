"""Per-bead cost telemetry (ISC-122..131).

Two properties matter most and are asserted explicitly:
  1. Telemetry can never fail real work (it is observability, not a dependency).
  2. An unknown cost must never render as a zero cost — that is the failure mode
     that would make an expensive route look free and corrupt a routing decision.
"""

import json

from agentco_harness.cost import (
    format_table,
    ledger_path,
    read_ledger,
    record_run,
    summarize,
)
from agentco_harness.executor import ExecResult, _parse_telemetry


def _result(**kw):
    base = dict(success=True, output="", error=None, exit_code=0, duration_seconds=1.0)
    base.update(kw)
    return ExecResult(**base)


# ------------------------------------------------------- envelope parsing


def test_parses_cost_usage_and_turns():
    t = _parse_telemetry({
        "total_cost_usd": 0.42,
        "num_turns": 7,
        "usage": {"input_tokens": 1000, "output_tokens": 250},
    })
    assert t == {"cost_usd": 0.42, "num_turns": 7, "input_tokens": 1000, "output_tokens": 250}


def test_model_used_is_the_highest_output_model_not_a_background_pass():
    """A cheap background pass also appears in modelUsage; the answer's author
    is the model that produced the most output."""
    t = _parse_telemetry({
        "modelUsage": {
            "claude-haiku-4-5-20251001": {"outputTokens": 40},
            "claude-opus-5": {"outputTokens": 9000},
        }
    })
    assert t["model_used"] == "claude-opus-5"


def test_empty_or_odd_envelope_yields_no_telemetry():
    assert _parse_telemetry({}) == {}
    assert _parse_telemetry({"total_cost_usd": "free"}) == {}
    assert _parse_telemetry([]) == {}


# --------------------------------------------------------------- ledger


def test_record_and_read_roundtrip(tmp_path):
    tasks = tmp_path / "tasks.jsonl"
    record_run(tasks, task_id="ac-1", agent="claude",
               exec_result=_result(cost_usd=0.10, model_used="claude-opus-5"),
               company="frontsteps", data_class="CONFIDENTIAL")
    entries = read_ledger(tasks)
    assert len(entries) == 1
    assert entries[0]["task_id"] == "ac-1"
    assert entries[0]["cost_usd"] == 0.10
    assert entries[0]["company"] == "frontsteps"


def test_ledger_sits_beside_the_queue(tmp_path):
    assert ledger_path(tmp_path / "tasks.jsonl") == tmp_path / "costs.jsonl"


def test_telemetry_failure_never_raises(tmp_path):
    """A ledger write that cannot succeed must degrade to a warning."""
    # Point the ledger at a path whose parent is a FILE, so mkdir/open fail.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    record_run(blocker / "nested" / "tasks.jsonl", task_id="ac-1",
               agent="claude", exec_result=_result())  # must not raise


def test_malformed_line_is_skipped_not_fatal(tmp_path):
    tasks = tmp_path / "tasks.jsonl"
    record_run(tasks, task_id="ac-1", agent="claude", exec_result=_result(cost_usd=1.0))
    with open(ledger_path(tasks), "a") as fh:
        fh.write("{torn line\n")
    record_run(tasks, task_id="ac-2", agent="claude", exec_result=_result(cost_usd=2.0))
    assert [e["task_id"] for e in read_ledger(tasks)] == ["ac-1", "ac-2"]


def test_failed_runs_are_recorded(tmp_path):
    """Omitting failures would bias every average optimistic."""
    tasks = tmp_path / "tasks.jsonl"
    record_run(tasks, task_id="ac-1", agent="zai",
               exec_result=_result(success=False, cost_usd=3.0))
    assert read_ledger(tasks)[0]["success"] is False


# ------------------------------------------------------------ summarize


def test_cost_per_completed_charges_failures_to_the_successes(tmp_path):
    """A model that burns money failing is not cheap. Two runs at $1 each,
    one success => $2.00 per completed bead, not $1.00."""
    tasks = tmp_path / "tasks.jsonl"
    record_run(tasks, task_id="a", agent="zai", exec_result=_result(cost_usd=1.0))
    record_run(tasks, task_id="b", agent="zai", exec_result=_result(success=False, cost_usd=1.0))
    row = summarize(read_ledger(tasks), group_by="agent")[0]
    assert row["cost_usd"] == 2.0
    assert row["completed"] == 1
    assert row["cost_per_completed"] == 2.0
    assert row["success_rate"] == 0.5


def test_unknown_cost_is_none_not_zero(tmp_path):
    """The load-bearing distinction: no completions means UNKNOWN cost-per-bead.
    Rendering that as 0.0 would make the worst route look like the best."""
    tasks = tmp_path / "tasks.jsonl"
    record_run(tasks, task_id="a", agent="zai", exec_result=_result(success=False, cost_usd=5.0))
    row = summarize(read_ledger(tasks), group_by="agent")[0]
    assert row["cost_per_completed"] is None
    assert "—" in format_table([row], "agent")


def test_unpriced_runs_are_surfaced_not_silently_zero(tmp_path):
    """Subscription-billed runs carry no price; a summary must say so rather
    than imply the work was free."""
    tasks = tmp_path / "tasks.jsonl"
    record_run(tasks, task_id="a", agent="claude", exec_result=_result())  # no cost_usd
    rows = summarize(read_ledger(tasks), group_by="agent")
    assert rows[0]["priced_runs"] == 0
    assert "carried no price" in format_table(rows, "agent")


def test_group_by_model_splits_correctly(tmp_path):
    tasks = tmp_path / "tasks.jsonl"
    record_run(tasks, task_id="a", agent="claude", exec_result=_result(cost_usd=1.0, model_used="claude-opus-5"))
    record_run(tasks, task_id="b", agent="claude", exec_result=_result(cost_usd=0.1, model_used="claude-sonnet-5"))
    rows = summarize(read_ledger(tasks), group_by="model_used")
    assert [r["model_used"] for r in rows] == ["claude-opus-5", "claude-sonnet-5"]


def test_empty_ledger_summarizes_cleanly(tmp_path):
    assert summarize(read_ledger(tmp_path / "tasks.jsonl"), "agent") == []
    assert "No cost telemetry" in format_table([], "agent")
