"""Executor tests: fake `claude` binary on PATH — success, non-zero exit,
and timeout are all recorded loudly, never silent, never hanging."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from agentco_harness import executor
from agentco_harness.executor import (
    extract_result_text,
    run_claude_task,
    run_forge_task,
    run_zai_store_backed_task,
)


def _fake_claude(tmp_path: Path, script_body: str) -> str:
    """Write an executable fake `claude` and return its path."""
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n" + script_body)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return str(binary)

def test_success_captures_stdout(tmp_path):
    claude = _fake_claude(tmp_path, 'echo \'{"result": "done"}\'\n')
    result = run_claude_task("do the thing", claude_bin=claude)
    assert result.success is True
    assert '"result": "done"' in result.output
    assert result.exit_code == 0
    assert result.error is None


def test_nonzero_exit_fails_loudly(tmp_path):
    claude = _fake_claude(tmp_path, 'echo "boom" >&2\nexit 3\n')
    result = run_claude_task("do the thing", claude_bin=claude)
    assert result.success is False
    assert result.exit_code == 3
    assert "exited 3" in result.error
    assert "boom" in result.error


def test_nonzero_exit_with_stderr_never_retries(tmp_path, monkeypatch):
    """A real tool/logic failure (has stderr) fails on the FIRST attempt —
    retry is reserved for the bare-exit transient signature only."""
    import agentco_harness.executor as executor_mod

    monkeypatch.setattr(executor_mod, "_BARE_EXIT_RETRY_BACKOFF_S", 0)
    counter = tmp_path / "calls"
    claude = _fake_claude(
        tmp_path,
        f'echo $$ >> {counter}\necho "boom" >&2\nexit 3\n',
    )
    result = run_claude_task("do the thing", claude_bin=claude)
    assert result.success is False
    assert counter.read_text().count("\n") == 1  # exactly one invocation, no retry


def test_bare_exit_retries_once_then_succeeds(tmp_path, monkeypatch):
    """First invocation bare-exits (transient crash signature: no stderr);
    the automatic retry succeeds — the caller never sees a failure."""
    import agentco_harness.executor as executor_mod

    monkeypatch.setattr(executor_mod, "_BARE_EXIT_RETRY_BACKOFF_S", 0)
    counter = tmp_path / "calls"
    counter.write_text("")
    claude = _fake_claude(
        tmp_path,
        f'echo x >> {counter}\n'
        f'N=$(wc -l < {counter})\n'
        f'if [ "$N" -eq 1 ]; then exit 1; fi\n'
        f'echo \'{{"result": "done"}}\'\n',
    )
    result = run_claude_task("do the thing", claude_bin=claude)
    assert result.success is True
    assert counter.read_text().count("\n") == 2  # first attempt + one retry


def test_bare_exit_retries_once_then_still_fails(tmp_path, monkeypatch):
    """Retry is bounded to exactly one attempt — a repeatable bare-exit
    failure surfaces loudly rather than retrying forever."""
    import agentco_harness.executor as executor_mod

    monkeypatch.setattr(executor_mod, "_BARE_EXIT_RETRY_BACKOFF_S", 0)
    counter = tmp_path / "calls"
    claude = _fake_claude(tmp_path, f"echo x >> {counter}\nexit 1\n")
    result = run_claude_task("do the thing", claude_bin=claude)
    assert result.success is False
    assert "retried once" in result.error
    assert counter.read_text().count("\n") == 2  # original + exactly one retry, no more


def test_timeout_fails_loudly_never_hangs(tmp_path):
    claude = _fake_claude(tmp_path, "sleep 5\n")
    result = run_claude_task("do the thing", claude_bin=claude, timeout=1)
    assert result.success is False
    assert "timed out after 1s" in result.error
    assert result.duration_seconds < 5


def test_missing_binary_fails_loudly(tmp_path):
    result = run_claude_task("x", claude_bin=str(tmp_path / "no-such-claude"))
    assert result.success is False
    assert "not found" in result.error


def test_budget_flags_passed(tmp_path):
    claude = _fake_claude(tmp_path, 'echo "$@"\n')
    result = run_claude_task("prompt here", max_turns=7, claude_bin=claude)
    assert "--max-turns 7" in result.output
    assert "--output-format json" in result.output


def test_claudecode_env_stripped(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    claude = _fake_claude(tmp_path, 'echo "CLAUDECODE=[${CLAUDECODE}]"\n')
    result = run_claude_task("x", claude_bin=claude)
    assert "CLAUDECODE=[]" in result.output


def test_zai_truncated_response_flags_failure(tmp_path):
    """The z.ai path routes through _run_proc, so a max_tokens-truncated
    response is caught as a failure (success=False, truncated=True) instead
    of being reported as a silent success."""
    claude = _fake_claude(
        tmp_path, 'echo \'{"result": "partial", "stop_reason": "max_tokens"}\'\n'
    )
    result = run_zai_store_backed_task(
        "task-123", claude_bin=claude, zai_api_key="zai-test-key"
    )
    assert result.success is False
    assert result.truncated is True
    assert "truncated" in result.error


def test_zai_success_not_truncated(tmp_path):
    """A well-formed z.ai response with no max_tokens stop still succeeds."""
    claude = _fake_claude(
        tmp_path, 'echo \'{"result": "done", "stop_reason": "end_turn"}\'\n'
    )
    result = run_zai_store_backed_task(
        "task-123", claude_bin=claude, zai_api_key="zai-test-key"
    )
    assert result.success is True
    assert result.truncated is False
    assert result.error is None


def test_resolve_claude_bin_absolute_even_without_path(monkeypatch):
    """A shelled-out claude must resolve to an absolute path so a launchd/cron env
    without ~/.local/bin on PATH doesn't fail 'not found' (RCA ac-abdf67e6 root cause)."""
    from agentco_harness.executor import _resolve_claude_bin, _base_cmd
    import os
    # an already-absolute path passes through
    assert _resolve_claude_bin("/opt/x/claude") == "/opt/x/claude"
    # a bare name resolves off PATH to the known fallback when PATH is empty
    monkeypatch.setenv("PATH", "")
    resolved = _resolve_claude_bin("claude")
    assert os.path.isabs(resolved)
    assert _base_cmd("claude", 1, None)[0] == resolved


# --- failure-cause reporting ------------------------------------------------
#
# Regression cover for the masked-cause defect (2026-07-18): five feeds ingest
# beads recorded "claude.ai connectors are disabled..." — a warning the CLI
# prints on EVERY run including successful ones — while the real cause was an
# HTTP 429 from z.ai reported in the stdout JSON. The RCA loop then spent Opus
# analyzing the warning.

CONNECTORS_WARNING = (
    "⚠ claude.ai connectors are disabled because ANTHROPIC_API_KEY or another "
    "auth source is set and takes precedence over your claude.ai login"
)


def test_meaningful_stderr_drops_advisory_warnings():
    assert executor._meaningful_stderr(CONNECTORS_WARNING) == ""
    assert executor._meaningful_stderr(f"{CONNECTORS_WARNING}\nreal failure") == "real failure"


def test_failure_names_the_real_cause_from_stdout_json():
    """The 429 that was masked: cause lives in stdout, warning lives in stderr."""
    stdout = json.dumps({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "api_error_status": 429,
        "result": 'Insufficient balance or no resource package. Please recharge.',
    })

    msg = executor._compose_failure("z.ai", 1, stdout, CONNECTORS_WARNING)

    assert "429" in msg
    assert "Insufficient balance" in msg
    assert "connectors are disabled" not in msg


def test_warning_only_stderr_is_not_passed_off_as_a_cause():
    msg = executor._compose_failure("z.ai", 1, "", CONNECTORS_WARNING)

    assert "connectors are disabled" not in msg
    assert "advisory warnings" in msg
    assert "cause not reported" in msg


def test_real_stderr_is_still_reported():
    msg = executor._compose_failure("claude", 2, "", "fatal: something broke")

    assert "fatal: something broke" in msg


def test_non_json_stdout_falls_back_to_a_bounded_tail():
    msg = executor._compose_failure("claude", 1, "segfault detail here", "")

    assert "segfault detail here" in msg


def test_huge_non_json_stdout_is_truncated():
    msg = executor._compose_failure("claude", 1, "x" * 5000, "")

    assert len(msg) < 700


# ------------------------------------------------------------ extract_result_text
# (the "raw JSON blob in the chat thread" defect — a chat reply's ExecResult.output
# is the WHOLE --output-format json envelope, not the agent's prose answer)


def test_extract_result_text_success_envelope():
    stdout = json.dumps({"type": "result", "subtype": "success", "result": "the answer is 42"})
    assert extract_result_text(stdout) == "the answer is 42"


def test_extract_result_text_strips_whitespace():
    stdout = json.dumps({"result": "  padded answer  \n"})
    assert extract_result_text(stdout) == "padded answer"


def test_extract_result_text_error_envelope_names_the_cause():
    """is_error envelopes must never surface as the raw 'result' string when
    that string is itself an error description — _stdout_failure_detail's
    api_error_status/subtype framing is more useful than the bare text."""
    stdout = json.dumps(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "api_error_status": 429,
            "result": "Insufficient balance or no resource package.",
        }
    )
    text = extract_result_text(stdout)
    assert "429" in text
    assert "Insufficient balance" in text


def test_extract_result_text_non_json_falls_back_to_raw():
    assert extract_result_text("plain text, not an envelope at all") == "plain text, not an envelope at all"


def test_extract_result_text_empty_or_garbage_is_never_a_crash():
    assert extract_result_text("") == ""
    assert extract_result_text("{not json") == "{not json"
    assert extract_result_text(json.dumps([1, 2, 3])) == "[1, 2, 3]"


def test_warning_only_stderr_still_counts_as_bare_exit_for_retry(monkeypatch, tmp_path):
    """A constant warning must not suppress the transient-crash retry."""
    calls = []

    def fake_run(*a, **kw):
        calls.append(1)
        return executor._ProcOutcome(
            returncode=1, stdout="", stderr=CONNECTORS_WARNING, idle_killed=False
        )

    # The subprocess seam is now _supervise (Popen + idle watchdog), not
    # subprocess.run — the retry contract it guards is unchanged.
    monkeypatch.setattr(executor, "_supervise", fake_run)
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)

    result = executor._run_proc(["claude"], "prompt", 10, "claude", label="z.ai")

    assert len(calls) == 2, "warning-only stderr should still trigger the one retry"
    assert result.success is False


# ---------------------------------------------------------------- forge/codex


def _fake_codex(tmp_path: Path, script_body: str) -> str:
    """Write an executable fake `codex` and return its path."""
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n" + script_body)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return str(binary)


def test_forge_success_captures_stdout(tmp_path):
    codex = _fake_codex(tmp_path, 'echo "FORGE_OK"\n')
    result = run_forge_task("do the thing", codex_bin=codex)
    assert result.success is True
    assert "FORGE_OK" in result.output
    assert result.exit_code == 0


def test_forge_nonzero_exit_fails_loudly(tmp_path):
    codex = _fake_codex(tmp_path, 'echo "codex blew up" >&2\nexit 4\n')
    result = run_forge_task("do the thing", codex_bin=codex)
    assert result.success is False
    assert result.exit_code == 4
    assert "codex blew up" in result.error
    assert "forge" in result.error


def test_forge_missing_binary_names_the_fix(tmp_path):
    """A missing codex must say how to install it, not raise FileNotFoundError
    out of a 01:00 cycle."""
    result = run_forge_task("x", codex_bin=str(tmp_path / "absent-codex"))
    assert result.success is False
    assert "codex binary not found" in result.error
    assert "codex login" in result.error


def test_forge_timeout_is_loud(tmp_path):
    codex = _fake_codex(tmp_path, "sleep 5\n")
    result = run_forge_task("x", codex_bin=codex, timeout=1)
    assert result.success is False
    assert "exceeded 1s timeout" in result.error
    assert result.exit_code is None


def test_forge_passes_model_and_skips_git_check(tmp_path):
    """--skip-git-repo-check must always be present: beads run from arbitrary
    working directories, and codex otherwise refuses outside a repo."""
    codex = _fake_codex(tmp_path, 'echo "$@"\n')
    result = run_forge_task("the prompt", codex_bin=codex, model="gpt-5.6-sol")
    assert "--skip-git-repo-check" in result.output
    assert "--model gpt-5.6-sol" in result.output
    assert "the prompt" in result.output
    assert result.model_used == "gpt-5.6-sol"


# ── Transient upstream API statuses (529 Overloaded and friends) ──────────────
# Regression guard for ac-3df8e12a: a 529 reports itself on stdout, which made
# the bare-exit gate consider it a "real" failure and skip the retry entirely.

_OVERLOAD_ENVELOPE = (
    '{"is_error": true, "subtype": "error_during_execution", '
    '"api_error_status": 529, '
    '"result": "API Error: 529 Overloaded. This is a server-side issue, '
    'usually temporary."}'
)


def test_transient_529_retries_then_succeeds(tmp_path, monkeypatch):
    """An overloaded upstream is temporary by definition — the executor tries
    again instead of failing the bead and spawning an RCA."""
    monkeypatch.setattr(executor, "_API_RETRY_BACKOFFS_S", (0, 0))
    counter = tmp_path / "calls"
    counter.write_text("")
    claude = _fake_claude(
        tmp_path,
        f"echo x >> {counter}\n"
        f"N=$(wc -l < {counter})\n"
        f"if [ \"$N\" -eq 1 ]; then echo '{_OVERLOAD_ENVELOPE}'; exit 1; fi\n"
        f"echo '{{\"result\": \"done\"}}'\n",
    )
    result = run_claude_task("do the thing", claude_bin=claude)
    assert result.success is True
    assert counter.read_text().count("\n") == 2  # first attempt + one retry


def test_transient_529_retry_is_bounded_and_fails_loudly(tmp_path, monkeypatch):
    """A sustained outage still fails — bounded to the configured backoffs, and
    the error names both the status and the fact that retries were spent."""
    monkeypatch.setattr(executor, "_API_RETRY_BACKOFFS_S", (0, 0))
    counter = tmp_path / "calls"
    claude = _fake_claude(
        tmp_path, f"echo x >> {counter}\necho '{_OVERLOAD_ENVELOPE}'\nexit 1\n"
    )
    result = run_claude_task("do the thing", claude_bin=claude)
    assert result.success is False
    assert "529" in result.error
    assert "persisted across 3 attempts" in result.error
    assert counter.read_text().count("\n") == 3  # original + exactly two retries


def test_non_transient_api_status_never_retries(tmp_path, monkeypatch):
    """A 401 is an answer, not a blip — retrying it just burns time."""
    monkeypatch.setattr(executor, "_API_RETRY_BACKOFFS_S", (0, 0))
    counter = tmp_path / "calls"
    envelope = (
        '{"is_error": true, "api_error_status": 401, '
        '"result": "API Error: 401 authentication_error"}'
    )
    claude = _fake_claude(tmp_path, f"echo x >> {counter}\necho '{envelope}'\nexit 1\n")
    result = run_claude_task("do the thing", claude_bin=claude)
    assert result.success is False
    assert counter.read_text().count("\n") == 1  # no retry
    assert "persisted across" not in result.error


def test_retryable_status_classifier():
    assert executor._retryable_api_status(_OVERLOAD_ENVELOPE) == 529
    assert executor._retryable_api_status('{"api_error_status": 429}') == 429
    assert executor._retryable_api_status('{"api_error_status": 503}') == 503
    assert executor._retryable_api_status('{"api_error_status": 400}') is None
    assert executor._retryable_api_status('{"result": "fine"}') is None
    assert executor._retryable_api_status("not json at all") is None
    assert executor._retryable_api_status('{"api_error_status": "boom"}') is None
