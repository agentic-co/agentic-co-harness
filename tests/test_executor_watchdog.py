"""Stalled-builder watchdog + completion marker.

Everything here runs against a FAKE `claude` binary (a shell script), never a
model: a script that sleeps without printing IS a stalled builder, which is the
only honest way to test a watchdog whose whole subject is silence.

The fake reproduces the two behaviours of the real CLI that this watchdog turns
on. It prints nothing until it exits (the real one buffers under
`--output-format json`), and it appends to a transcript at the `--session-id`
it was handed. A stalled builder is therefore a fake that stops touching its
transcript — NOT merely one that stops printing, which is what a healthy child
looks like on this path.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import agentco_harness.orchestrator as orchestrator_mod
from agentco_harness.beads import TaskResult, TaskStatus
from agentco_harness.config import DEFAULT_IDLE_TIMEOUT_S, Config
from agentco_harness.executor import (
    COMPLETION_MARKER,
    ExecResult,
    _detect_completion_marker,
    _find_transcript,
    _resolve_idle_timeout,
    _transcript_root,
    run_store_backed_task,
)
from agentco_harness.orchestrator import Orchestrator


def _fake_claude(tmp_path: Path, script_body: str) -> str:
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n" + script_body)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return str(binary)


# Parses out the --session-id the executor pinned and exports it as $TRANSCRIPT,
# the path the real CLI would write, so a fake can act alive or act stalled.
_TRANSCRIPT_PREAMBLE = """\
for a in "$@"; do
  if [ "$prev" = "--session-id" ]; then SID="$a"; fi
  prev="$a"
done
TDIR="$CLAUDE_CONFIG_DIR/projects/-fake-node"
mkdir -p "$TDIR"
TRANSCRIPT="$TDIR/$SID.jsonl"
"""


def _fake_claude_with_transcript(tmp_path: Path, script_body: str) -> str:
    """A fake that knows where its transcript goes (see _TRANSCRIPT_PREAMBLE)."""
    return _fake_claude(tmp_path, _TRANSCRIPT_PREAMBLE + script_body)


def _isolate_config_dir(monkeypatch, tmp_path: Path) -> Path:
    """Point both the child and the probe at a throwaway ~/.claude."""
    root = tmp_path / "cfg"
    (root / "projects").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    return root


def _orch(tmp_path: Path) -> Orchestrator:
    config = Config()
    config.tasks_path = str(tmp_path / "tasks.jsonl")
    config.notify.enabled = False
    return Orchestrator(config)


# --- idle timeout -----------------------------------------------------------


def test_stalled_child_is_killed_at_the_idle_timeout(tmp_path, monkeypatch):
    # Writes its transcript once, then goes dark on BOTH signals: a real stall.
    _isolate_config_dir(monkeypatch, tmp_path)
    claude = _fake_claude_with_transcript(tmp_path, 'echo start > "$TRANSCRIPT"\nsleep 30\n')
    result = run_store_backed_task("ac-11111111", claude_bin=claude, idle_timeout_s=1)
    assert result.success is False
    assert result.idle_timeout_hit is True
    assert result.error == "idle timeout after 1s — no output and no transcript activity"
    # Killed, not waited out: the 30s sleep never completed.
    assert result.duration_seconds < 15


def test_transcript_activity_keeps_a_silent_child_alive(tmp_path, monkeypatch):
    """The regression test for ac-4f095dcf / ac-1be082d5.

    A child that prints NOTHING for well past the idle window while steadily
    appending to its transcript is the exact shape of the two beads the
    watchdog killed — busy, mid-tool-call, and silent on stdout because
    `--output-format json` buffers until exit. It must survive, and its output
    must still arrive whole at the end.
    """
    _isolate_config_dir(monkeypatch, tmp_path)
    claude = _fake_claude_with_transcript(
        tmp_path,
        'i=0\nwhile [ $i -lt 8 ]; do echo working >> "$TRANSCRIPT"; sleep 0.5; i=$((i+1)); done\n'
        "echo '{\"result\": \"finished anyway\"}'\n",
    )
    result = run_store_backed_task("ac-22222222", claude_bin=claude, idle_timeout_s=1)
    assert result.idle_timeout_hit is False
    assert result.success is True
    assert json.loads(result.output)["result"] == "finished anyway"


def test_stream_output_alone_still_keeps_the_watchdog_at_bay(tmp_path, monkeypatch):
    # No transcript at all, but a chatty child: the stream stamp alone must
    # carry it, without the probe ever being consulted.
    _isolate_config_dir(monkeypatch, tmp_path)
    claude = _fake_claude(
        tmp_path,
        "i=0\nwhile [ $i -lt 4 ]; do echo tick; sleep 0.5; i=$((i+1)); done\n",
    )
    result = run_store_backed_task("ac-2222aaaa", claude_bin=claude, idle_timeout_s=1)
    assert result.success is True
    assert result.idle_timeout_hit is False
    assert "tick" in result.output


def test_no_transcript_at_all_holds_fire_rather_than_killing(tmp_path, monkeypatch, capsys):
    """Fail OPEN. With no transcript there is no evidence of a stall, and the
    whole-run timeout is the budget that was actually configured."""
    _isolate_config_dir(monkeypatch, tmp_path)
    claude = _fake_claude(tmp_path, "sleep 3\necho '{\"result\": \"survived\"}'\n")
    result = run_store_backed_task("ac-3333bbbb", claude_bin=claude, idle_timeout_s=1)
    assert result.idle_timeout_hit is False
    assert result.success is True
    assert "no transcript to read" in capsys.readouterr().out


def test_watchdog_disabled_by_zero_lets_a_silent_child_run(tmp_path):
    claude = _fake_claude(tmp_path, "sleep 1\necho done\n")
    result = run_store_backed_task("ac-33333333", claude_bin=claude, idle_timeout_s=0)
    assert result.success is True
    assert result.idle_timeout_hit is False


def test_idle_kill_is_not_retried_as_a_transient_bare_exit(tmp_path, monkeypatch, capsys):
    _isolate_config_dir(monkeypatch, tmp_path)
    claude = _fake_claude_with_transcript(tmp_path, 'echo start > "$TRANSCRIPT"\nsleep 30\n')
    run_store_backed_task("ac-44444444", claude_bin=claude, idle_timeout_s=1)
    assert "retrying once" not in capsys.readouterr().out


# --- session id: how the watchdog finds the transcript ----------------------


def test_armed_watchdog_pins_a_session_id(tmp_path, monkeypatch):
    _isolate_config_dir(monkeypatch, tmp_path)
    claude = _fake_claude(tmp_path, 'echo "$@" > ' + str(tmp_path / "args.txt") + "\n")
    run_store_backed_task("ac-5555cccc", claude_bin=claude, idle_timeout_s=5)
    assert "--session-id" in (tmp_path / "args.txt").read_text()


def test_disarmed_watchdog_pins_nothing(tmp_path):
    # Nothing else needs the session id, so it is not imposed when unused.
    claude = _fake_claude(tmp_path, 'echo "$@" > ' + str(tmp_path / "args.txt") + "\n")
    run_store_backed_task("ac-6666dddd", claude_bin=claude, idle_timeout_s=0)
    assert "--session-id" not in (tmp_path / "args.txt").read_text()


def test_bare_exit_retry_mints_a_fresh_session_id(tmp_path, monkeypatch):
    """The CLI rejects a reused id outright ("Session ID <uuid> is already in
    use.", exit 1), so a retry that replayed the first id would turn every
    transient crash into a hard failure."""
    _isolate_config_dir(monkeypatch, tmp_path)
    log = tmp_path / "ids.txt"
    # Bare non-zero exit (no stderr) on the first attempt triggers the retry.
    claude = _fake_claude_with_transcript(tmp_path, f'echo "$SID" >> {log}\nexit 1\n')
    monkeypatch.setattr("agentco_harness.executor._BARE_EXIT_RETRY_BACKOFF_S", 0)
    run_store_backed_task("ac-7777eeee", claude_bin=claude, idle_timeout_s=5)
    ids = log.read_text().split()
    assert len(ids) == 2, "expected one retry"
    assert ids[0] != ids[1]


def test_transcript_is_found_regardless_of_the_project_dir_name(tmp_path):
    """Located by session id, not by re-deriving the CLI's cwd->slug rule."""
    root = tmp_path / "projects"
    (root / "-some-Weird-realpath--slug").mkdir(parents=True)
    wanted = root / "-some-Weird-realpath--slug" / "abc-123.jsonl"
    wanted.write_text("{}\n")
    assert _find_transcript(root, "abc-123") == wanted
    assert _find_transcript(root, "no-such-session") is None


def test_transcript_root_follows_the_isolated_config_dir():
    assert _transcript_root({"CLAUDE_CONFIG_DIR": "/tmp/iso"}) == Path("/tmp/iso/projects")
    assert _transcript_root({}).name == "projects"


def test_stdout_is_still_captured_whole_on_a_normal_run(tmp_path):
    claude = _fake_claude(tmp_path, 'echo \'{"result": "hello"}\'\n')
    result = run_store_backed_task("ac-55555555", claude_bin=claude, idle_timeout_s=5)
    assert json.loads(result.output)["result"] == "hello"


# --- idle timeout config ----------------------------------------------------


def test_idle_timeout_defaults_when_no_config(tmp_path):
    assert _resolve_idle_timeout(None) == DEFAULT_IDLE_TIMEOUT_S


def test_idle_timeout_reads_the_node_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("tasks_path: tasks.jsonl\nexecutor:\n  idle_timeout_s: 42\n")
    assert _resolve_idle_timeout(str(cfg)) == 42
    assert Config.load(cfg).executor.idle_timeout_s == 42


def test_idle_timeout_zero_in_config_disables_the_watchdog(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("tasks_path: tasks.jsonl\nexecutor:\n  idle_timeout_s: 0\n")
    assert _resolve_idle_timeout(str(cfg)) == 0


def test_malformed_idle_timeout_warns_and_keeps_the_watchdog_armed(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("tasks_path: tasks.jsonl\nexecutor:\n  idle_timeout_s: soon\n")
    config = Config.load(cfg)
    assert config.executor.idle_timeout_s == DEFAULT_IDLE_TIMEOUT_S
    assert "idle_timeout_s" in capsys.readouterr().out


def test_unknown_executor_key_is_warned_about(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("tasks_path: tasks.jsonl\nexecutor:\n  idle_timeout: 60\n")
    Config.load(cfg)
    assert "nothing consumes" in capsys.readouterr().out


# --- completion marker ------------------------------------------------------


def test_prompt_instructs_the_agent_to_emit_the_marker(tmp_path):
    claude = _fake_claude(tmp_path, "cat\n")
    result = run_store_backed_task("ac-66666666", claude_bin=claude, idle_timeout_s=0)
    assert COMPLETION_MARKER in result.output


def test_marker_detected_inside_the_json_envelope():
    envelope = json.dumps({"result": f"did stuff\n{COMPLETION_MARKER} wired the webhook"})
    assert _detect_completion_marker(envelope) == "wired the webhook"


def test_marker_detected_in_plain_stdout():
    assert _detect_completion_marker(f"blah\n{COMPLETION_MARKER}   shipped it\n") == "shipped it"


def test_last_marker_wins_over_a_quoted_instruction():
    text = f"I must end with {COMPLETION_MARKER} <summary>\n{COMPLETION_MARKER} real result"
    assert _detect_completion_marker(text) == "real result"


def test_missing_marker_reads_as_none():
    assert _detect_completion_marker('{"result": "I finished the work."}') is None


def test_marker_surfaces_on_the_exec_result(tmp_path):
    claude = _fake_claude(
        tmp_path,
        f"cat > /dev/null\necho '{COMPLETION_MARKER} built the thing'\n",
    )
    result = run_store_backed_task("ac-77777777", claude_bin=claude, idle_timeout_s=0)
    assert result.completion_marker == "built the thing"


# --- orchestrator: what the marker does to the bead -------------------------


def test_marker_oneliner_fills_an_empty_result(tmp_path, monkeypatch):
    orch = _orch(tmp_path)
    task = orch.beads.create("work", "d", assigned_agent="claude", metadata={"store_backed": True})

    def fake(task_id, config_path, timeout, max_turns, model=None):
        orch.beads.update(task_id, status=TaskStatus.DONE)  # done, but no result written
        return ExecResult(True, "", None, 0, 0.1, completion_marker="patched the parser")

    monkeypatch.setattr(orchestrator_mod, "run_store_backed_task", fake)
    assert orch._execute_claude_task(task) is True
    assert orch.beads.get(task.id).result == "patched the parser"


def test_marker_never_overwrites_a_richer_result(tmp_path, monkeypatch):
    orch = _orch(tmp_path)
    task = orch.beads.create("work", "d", assigned_agent="claude", metadata={"store_backed": True})
    rich = TaskResult(status="complete", output="the full deliverable").to_json()

    def fake(task_id, config_path, timeout, max_turns, model=None):
        orch.beads.complete(task_id, result=rich)
        return ExecResult(True, "", None, 0, 0.1, completion_marker="thin summary")

    monkeypatch.setattr(orchestrator_mod, "run_store_backed_task", fake)
    assert orch._execute_claude_task(task) is True
    assert orch.beads.get(task.id).result == rich


def test_missing_marker_flags_the_bead_and_warns_but_does_not_fail_it(tmp_path, monkeypatch, capsys):
    orch = _orch(tmp_path)
    task = orch.beads.create("work", "d", assigned_agent="claude", metadata={"store_backed": True})

    def fake(task_id, config_path, timeout, max_turns, model=None):
        orch.beads.complete(task_id, result="did it")
        return ExecResult(True, "", None, 0, 0.1)  # no marker

    monkeypatch.setattr(orchestrator_mod, "run_store_backed_task", fake)
    assert orch._execute_claude_task(task) is True
    refreshed = orch.beads.get(task.id)
    assert refreshed.status == TaskStatus.DONE  # measured, not enforced
    assert refreshed.metadata["completion_marker"] == "missing"
    assert "completion_marker=missing" in capsys.readouterr().out


def test_idle_timeout_fails_the_bead_not_verify_failed(tmp_path, monkeypatch):
    orch = _orch(tmp_path)
    task = orch.beads.create(
        "work",
        "d",
        assigned_agent="claude",
        metadata={
            "store_backed": True,
            "verify": {"class": "deterministic", "check": "true"},
        },
    )

    monkeypatch.setattr(
        orchestrator_mod,
        "run_store_backed_task",
        lambda task_id, config_path, timeout, max_turns, model=None: ExecResult(
            False,
            "",
            "idle timeout after 900s — no output and no transcript activity",
            None,
            0.1,
            idle_timeout_hit=True,
        ),
    )
    assert orch._execute_claude_task(task) is False
    refreshed = orch.beads.get(task.id)
    assert refreshed.status == TaskStatus.FAILED
    assert refreshed.status != TaskStatus.VERIFY_FAILED
    assert "idle timeout after 900s" in (refreshed.result or "")
