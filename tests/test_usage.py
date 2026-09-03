"""Usage metering: one row per execution, mandatory attribution, NULL not zero.

Everything here runs against a FAKE executor — a stub `claude` script on disk or
a plain callable. No network, no API key, no real model.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from agentco_harness import usage
from agentco_harness.executor import ExecResult, run_claude_task, run_forge_task
from agentco_harness.usage import Attribution, MissingAttribution


# --------------------------------------------------------------- fake helpers


def _fake_claude(tmp_path, envelope: dict, exit_code: int = 0):
    """A stub `claude` that prints one `--output-format json` envelope."""
    script = tmp_path / "fake-claude"
    script.write_text(
        "#!/bin/sh\ncat > /dev/null\n"
        f"cat <<'EOF'\n{json.dumps(envelope)}\nEOF\n"
        f"exit {exit_code}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _ledger_rows(tasks_path):
    return usage.read_ledger(tasks_path)


@pytest.fixture()
def node(tmp_path):
    """A node store path — the ledger lands beside it."""
    return tmp_path / "acme" / ".agentco" / "tasks.jsonl"


# ------------------------------------------------------- attribution is required


def test_meter_refuses_to_invoke_without_attribution(tmp_path):
    """The guard that makes the meter worth anything: no attribution, no model.

    The conftest installs a suite-wide attribution, so this test clears it to
    stand where a real un-instrumented call site stands.
    """
    ran = []
    token = usage._CURRENT.set(None)
    try:
        with pytest.raises(MissingAttribution) as e:
            usage.meter(lambda: ran.append(1), executor="claude", route="anthropic")
    finally:
        usage._CURRENT.reset(token)
    assert "attributed" in str(e.value)
    assert ran == [], "the model must not be invoked before attribution is proven"


def test_executor_refuses_to_spawn_without_attribution(tmp_path):
    """The refusal reaches the real subprocess boundary, not just the helper."""
    claude = _fake_claude(tmp_path, {"result": "hi"})
    token = usage._CURRENT.set(None)
    try:
        with pytest.raises(MissingAttribution):
            run_claude_task("do the thing", claude_bin=claude)
    finally:
        usage._CURRENT.reset(token)


@pytest.mark.parametrize("missing", ["bead_id", "lane", "tasks_path"])
def test_incomplete_attribution_raises(missing, node):
    fields = {"bead_id": "ac-1", "lane": "cycle", "tasks_path": str(node)}
    fields[missing] = ""
    with pytest.raises(MissingAttribution) as e:
        Attribution(**fields)
    assert missing in str(e.value)


def test_attribution_context_nests_and_restores(node):
    with usage.attributed(bead_id="ac-outer", lane="cycle", tasks_path=str(node)):
        assert usage.current().bead_id == "ac-outer"
        with usage.attributed(bead_id="ac-inner", lane="planner", tasks_path=str(node)):
            assert usage.current().bead_id == "ac-inner"
        assert usage.current().bead_id == "ac-outer"


# ------------------------------------------------------ exactly one row per run


def test_one_execution_writes_exactly_one_row(tmp_path, node):
    claude = _fake_claude(
        tmp_path,
        {
            "result": "done",
            "total_cost_usd": 0.25,
            "num_turns": 3,
            "usage": {"input_tokens": 1200, "output_tokens": 340},
            "modelUsage": {"claude-sonnet-5": {"outputTokens": 340}},
        },
    )
    with usage.attributed(
        bead_id="ac-abc123",
        lane="cycle",
        tasks_path=str(node),
        company="acme",
        requested_model="sonnet",
    ):
        result = run_claude_task("go", claude_bin=claude)

    assert result.success
    rows = _ledger_rows(node)
    assert len(rows) == 1
    row = rows[0]
    assert row["bead_id"] == "ac-abc123"
    assert row["lane"] == "cycle"
    assert row["node"] == "acme"  # from <repo>/.agentco/tasks.jsonl
    assert row["executor"] == "claude"
    assert row["route"] == "anthropic"
    assert row["model_requested"] == "sonnet"
    assert row["model_used"] == "claude-sonnet-5"
    assert row["exit_status"] == "ok"
    assert row["input_tokens"] == 1200
    assert row["output_tokens"] == 340
    assert row["total_tokens"] == 1540
    assert row["cost_usd"] == 0.25
    assert row["num_turns"] == 3
    assert row["schema"] == usage.SCHEMA


def test_ledger_lands_beside_the_task_store(node):
    with usage.attributed(bead_id="ac-1", lane="cycle", tasks_path=str(node)):
        usage.meter(
            lambda: ExecResult(True, "", None, 0, 1.0), executor="claude", route="anthropic"
        )
    assert (node.parent / "usage.jsonl").is_file()
    assert usage.ledger_path(node).name == "usage.jsonl"


def test_two_executions_write_two_rows(node):
    for bead in ("ac-1", "ac-2"):
        with usage.attributed(bead_id=bead, lane="cycle", tasks_path=str(node)):
            usage.meter(
                lambda: ExecResult(True, "", None, 0, 1.0),
                executor="claude",
                route="anthropic",
            )
    assert [r["bead_id"] for r in _ledger_rows(node)] == ["ac-1", "ac-2"]


def test_internal_retry_is_still_one_row(tmp_path, node, monkeypatch):
    """A bare-exit retry is one bead's work retried, not two units of work.

    Two rows here would double every run count a cost review divides by.
    """
    monkeypatch.setattr("agentco_harness.executor._BARE_EXIT_RETRY_BACKOFF_S", 0)
    script = tmp_path / "flaky-claude"
    marker = tmp_path / "attempts"
    script.write_text(
        "#!/bin/sh\ncat > /dev/null\n"
        f'echo x >> "{marker}"\n'
        f'if [ "$(wc -l < "{marker}")" -lt 2 ]; then exit 1; fi\n'
        "echo '{\"result\": \"ok\"}'\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    with usage.attributed(bead_id="ac-retry", lane="cycle", tasks_path=str(node)):
        result = run_claude_task("go", claude_bin=str(script))

    assert result.success
    assert marker.read_text().count("x") == 2, "the retry must actually have happened"
    assert len(_ledger_rows(node)) == 1


# --------------------------------------------------------- NULL, never zero


def test_unknown_token_counts_are_null_not_zero(tmp_path, node):
    """A route that reports nothing must not look like a route that used nothing."""
    claude = _fake_claude(tmp_path, {"result": "done"})  # no usage, no cost
    with usage.attributed(bead_id="ac-null", lane="cycle", tasks_path=str(node)):
        run_claude_task("go", claude_bin=claude)

    raw = json.loads((node.parent / "usage.jsonl").read_text().strip())
    for field in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "total_tokens",
        "cost_usd",
    ):
        assert raw[field] is None, f"{field} must be null, got {raw[field]!r}"


def test_forge_route_records_null_tokens_not_zero(tmp_path, node):
    codex = tmp_path / "fake-codex"
    codex.write_text("#!/bin/sh\necho 'forge output'\n")
    codex.chmod(codex.stat().st_mode | stat.S_IEXEC)

    with usage.attributed(bead_id="ac-forge", lane="forge", tasks_path=str(node)):
        result = run_forge_task("go", codex_bin=str(codex))

    assert result.success
    (row,) = _ledger_rows(node)
    assert row["executor"] == "forge"
    assert row["route"] == "openai-codex"
    assert row["input_tokens"] is None and row["output_tokens"] is None
    assert row["cost_usd"] is None


def test_cache_tokens_are_lifted_when_reported(tmp_path, node):
    claude = _fake_claude(
        tmp_path,
        {
            "result": "done",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 30,
            },
        },
    )
    with usage.attributed(bead_id="ac-cache", lane="cycle", tasks_path=str(node)):
        run_claude_task("go", claude_bin=claude)
    (row,) = _ledger_rows(node)
    assert row["cache_read_tokens"] == 900
    assert row["cache_creation_tokens"] == 30


# ------------------------------------------------------------ failure recording


def test_failed_run_is_recorded_too(tmp_path, node):
    """An expensive failure is precisely what a cost review needs to see."""
    claude = _fake_claude(tmp_path, {"is_error": True, "result": "boom"}, exit_code=2)
    with usage.attributed(bead_id="ac-fail", lane="cycle", tasks_path=str(node)):
        result = run_claude_task("go", claude_bin=claude)
    assert not result.success
    (row,) = _ledger_rows(node)
    assert row["exit_status"] == "failed"
    assert row["exit_code"] == 2
    assert "boom" in (row["error"] or "")


def test_exception_out_of_the_run_is_recorded_then_reraised(node):
    def blows_up():
        raise RuntimeError("subprocess exploded")

    with usage.attributed(bead_id="ac-boom", lane="cycle", tasks_path=str(node)):
        with pytest.raises(RuntimeError, match="subprocess exploded"):
            usage.meter(blows_up, executor="claude", route="anthropic")
    (row,) = _ledger_rows(node)
    assert row["exit_status"] == "error"
    assert "RuntimeError" in row["error"]


def test_ledger_write_failure_never_fails_the_run(tmp_path, capsys):
    """Telemetry must not be load-bearing."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file")
    with usage.attributed(
        bead_id="ac-1", lane="cycle", tasks_path=str(blocker / "nested" / "tasks.jsonl")
    ):
        result = usage.meter(
            lambda: ExecResult(True, "ok", None, 0, 1.0),
            executor="claude",
            route="anthropic",
        )
    assert result.success
    assert "could not record usage" in capsys.readouterr().out


def test_malformed_ledger_line_is_skipped_not_fatal(node):
    node.parent.mkdir(parents=True, exist_ok=True)
    usage.ledger_path(node).write_text('{"bead_id": "ac-1"}\nnot json at all\n\n')
    assert [r["bead_id"] for r in usage.read_ledger(node)] == ["ac-1"]


# ------------------------------------------------------------------ reporting


def _row(**kw):
    base = {
        "at": "2026-08-25T10:00:00+00:00",
        "bead_id": "ac-1",
        "lane": "cycle",
        "node": "acme",
        "executor": "claude",
        "route": "anthropic",
        "model_used": "claude-sonnet-5",
        "exit_status": "ok",
        "duration_seconds": 10.0,
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
    }
    base.update(kw)
    return base


def test_summarize_by_day_model_bead_node():
    rows = [
        _row(at="2026-08-25T01:00:00+00:00", cost_usd=1.0, input_tokens=10, output_tokens=5),
        _row(at="2026-08-26T01:00:00+00:00", bead_id="ac-2", model_used="claude-haiku-4-5",
             cost_usd=0.1, node="recorro"),
    ]
    assert {r["day"] for r in usage.summarize(rows, "day")} == {"2026-08-25", "2026-08-26"}
    assert {r["model"] for r in usage.summarize(rows, "model")} == {
        "claude-sonnet-5",
        "claude-haiku-4-5",
    }
    assert {r["bead"] for r in usage.summarize(rows, "bead")} == {"ac-1", "ac-2"}
    assert {r["node"] for r in usage.summarize(rows, "node")} == {"acme", "recorro"}


def test_summary_keeps_unreported_tokens_null():
    """A group where nothing reported tokens is unknown, not zero."""
    (group,) = usage.summarize([_row(), _row()], "day")
    assert group["runs"] == 2
    assert group["input_tokens"] is None
    assert group["output_tokens"] is None
    assert group["cost_usd"] is None
    assert group["priced_runs"] == 0


def test_totals_separate_unreported_from_zero():
    t = usage.totals([_row(cost_usd=2.0, input_tokens=100, output_tokens=50), _row()])
    assert t["runs"] == 2
    assert t["cost_usd"] == 2.0
    assert t["priced_runs"] == 1
    assert t["unreported_cost_runs"] == 1
    assert t["input_tokens"] == 100


def test_format_table_renders_dash_for_unreported():
    text = usage.format_table(usage.summarize([_row()], "day"), "day")
    assert "—" in text
    assert "reported no price" in text


def test_format_table_empty_says_so():
    assert "No usage telemetry recorded yet" in usage.format_table([], "day")


def test_within_filters_by_age():
    from datetime import datetime, timezone

    now = datetime(2026, 8, 26, tzinfo=timezone.utc)
    rows = [_row(at="2026-08-25T00:00:00+00:00"), _row(at="2026-01-01T00:00:00+00:00")]
    assert len(usage.within(rows, 7, now=now)) == 1
    assert len(usage.within(rows, None, now=now)) == 2


def test_within_keeps_rows_with_unparseable_timestamps():
    """Dropping them would quietly shrink a total."""
    from datetime import datetime, timezone

    rows = [_row(at="not-a-date")]
    assert usage.within(rows, 7, now=datetime(2026, 8, 26, tzinfo=timezone.utc)) == rows


# ------------------------------------------------------------ the gap detector


def test_unmetered_beads_finds_executions_with_no_row():
    rows = [_row(bead_id="ac-1")]
    assert usage.unmetered_beads(rows, ["ac-1", "ac-2", "ac-3"]) == ["ac-2", "ac-3"]


def test_unmetered_beads_empty_when_everything_is_metered():
    rows = [_row(bead_id="ac-1"), _row(bead_id="ac-2")]
    assert usage.unmetered_beads(rows, ["ac-1", "ac-2"]) == []


def test_node_name_from_store_location(tmp_path):
    assert usage.node_name(tmp_path / "acme" / ".agentco" / "tasks.jsonl") == "acme"
    assert usage.node_name(tmp_path / "agentco" / "tasks.jsonl") == "agentco"


# ------------------------------------------------------------------- the CLI


def test_usage_cli_reports_totals_and_json(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from agentco_harness.cli import main

    root = tmp_path / "node"
    root.mkdir()
    (root / "tasks.jsonl").write_text("")
    (root / "config.yaml").write_text(f"tasks_path: {root / 'tasks.jsonl'}\n")
    usage.ledger_path(root / "tasks.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in (
                _row(cost_usd=1.5, input_tokens=100, output_tokens=20),
                _row(bead_id="ac-2"),
            )
        )
        + "\n"
    )

    runner = CliRunner()
    res = runner.invoke(main, ["-c", str(root / "config.yaml"), "usage", "--by", "bead"])
    assert res.exit_code == 0, res.output
    assert "ac-1" in res.output and "ac-2" in res.output

    res = runner.invoke(main, ["-c", str(root / "config.yaml"), "usage", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["totals"]["runs"] == 2
    assert payload["totals"]["cost_usd"] == 1.5
    assert payload["totals"]["unreported_cost_runs"] == 1


# ------------------------------------------- dispatch sites carry attribution


def _orch(tmp_path):
    from agentco_harness.config import Config
    from agentco_harness.orchestrator import Orchestrator

    config = Config()
    config.tasks_path = str(tmp_path / "acme" / ".agentco" / "tasks.jsonl")
    os.makedirs(os.path.dirname(config.tasks_path), exist_ok=True)
    config.notify.enabled = False
    return Orchestrator(config)


@pytest.mark.parametrize(
    "fn_name,metadata,lane",
    [
        ("run_store_backed_task", {"store_backed": True}, "cycle"),
        ("run_claude_task", {}, "cycle"),
    ],
)
def test_cycle_dispatch_opens_an_attribution(tmp_path, monkeypatch, fn_name, metadata, lane):
    """The meter is only worth its weakest call site — assert the site declares.

    Before this backport the dispatcher passed nothing at all; a fake that reads
    `usage.current()` proves the context is live at the moment the executor
    would spawn.
    """
    import agentco_harness.orchestrator as orchestrator_mod

    orch = _orch(tmp_path)
    task = orch.beads.create(
        "work", "d", assigned_agent="claude", metadata={**metadata, "company": "acme"}
    )
    seen = {}

    def fake(*args, **kwargs):
        seen["attribution"] = usage.current()
        if metadata.get("store_backed"):
            orch.beads.complete(task.id, result="done")
        return ExecResult(True, "out", None, 0, 1.0)

    monkeypatch.setattr(orchestrator_mod, fn_name, fake)
    assert orch._execute_claude_task(task) is True

    attribution = seen["attribution"]
    assert attribution is not None, f"{fn_name} was dispatched with no attribution in scope"
    assert attribution.bead_id == task.id
    assert attribution.lane == lane
    assert attribution.company == "acme"
    assert attribution.tasks_path == orch.config.tasks_path


def test_forge_dispatch_opens_an_attribution(tmp_path, monkeypatch):
    import agentco_harness.orchestrator as orchestrator_mod

    orch = _orch(tmp_path)
    task = orch.beads.create("work", "d", assigned_agent="forge")
    seen = {}

    def fake(*args, **kwargs):
        seen["attribution"] = usage.current()
        return ExecResult(True, "out", None, 0, 1.0)

    monkeypatch.setattr(orchestrator_mod, "run_forge_task", fake)
    assert orch._execute_forge_task(task) is True
    assert seen["attribution"].lane == "forge"
    assert seen["attribution"].bead_id == task.id
