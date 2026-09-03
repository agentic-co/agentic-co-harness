"""The SSH forced-command gate on the hub's pull lane (bead ac-b8700758).

`scripts/two-machine/agentco-pull-forced-command.sh` is the only thing standing
between the MacBook worker's SSH key and a full login shell on the machine that
holds every venture's bead store. Its input, `$SSH_ORIGINAL_COMMAND`, is chosen
entirely by whoever holds that key.

So the assertions here are mostly about what the wrapper REFUSES. An allowlist
that quietly grows a passthrough case is indistinguishable from no allowlist at
all, and the failure is silent by construction — the lane keeps working. Each
denial below therefore names the specific thing it stops:

* a general shell (`rm -rf`, `agentco tasks list`) — the reason forced commands
  exist at all;
* shell metacharacters — the injection class, denied before parsing rather than
  survived during it;
* `--config` — a remote key retargeting the hub at another company's store;
* `--force` — the remote worker switching off the reconcile-before-replay guard
  that exists to stop it double-writing ADO (Plans/TwoMachineLifeos.md).

The wrapper's dry-run hook prints the argv it WOULD execute, so every allow case
also asserts that `--config` is pinned by the hub and never taken from the
client. Runbook: Plans/TwoMachineSetupRunbook.md B3.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WRAPPER = REPO / "scripts" / "two-machine" / "agentco-pull-forced-command.sh"

DENIED = 77  # EX_NOPERM — the allowlist said no

PULL_OK = "agentco pull --agent frontsteps-worker --node frontsteps --max 3"
REPORT_OK = "agentco report ac-b8700758 --attempt 1 --failed"


@pytest.fixture
def hub(tmp_path):
    """A fake hub: a config file to pin to and an audit log to inspect."""
    config = tmp_path / "config.yaml"
    config.write_text("instance: portfolio\ntasks_path: tasks.jsonl\n")
    return {
        "config": config,
        "audit": tmp_path / "pull-audit.log",
        "bin": tmp_path / "agentco",
    }


def run(command: str | None, hub, dry_run: bool = True, **env_extra):
    """Invoke the wrapper exactly as sshd would: input via SSH_ORIGINAL_COMMAND."""
    env = dict(os.environ)
    env.update(
        {
            "AGENTCO_HUB_CONFIG": str(hub["config"]),
            "AGENTCO_PULL_AUDIT_LOG": str(hub["audit"]),
            "AGENTCO_BIN": str(hub["bin"]),
            "SSH_CONNECTION": "10.0.0.9 51000 10.0.0.2 22",
        }
    )
    if dry_run:
        env["AGENTCO_PULL_DRY_RUN"] = "1"
    env.update(env_extra)
    if command is None:
        env.pop("SSH_ORIGINAL_COMMAND", None)
    else:
        env["SSH_ORIGINAL_COMMAND"] = command
    return subprocess.run(
        [str(WRAPPER)], env=env, capture_output=True, text=True, timeout=30
    )


def argv_of(proc) -> list[str]:
    return proc.stdout.strip().splitlines()


def audit_lines(hub) -> list[str]:
    if not hub["audit"].exists():
        return []
    return [ln for ln in hub["audit"].read_text().splitlines() if ln.strip()]


def test_wrapper_is_executable():
    """A forced command sshd cannot execute fails open into the user's shell."""
    assert WRAPPER.exists(), f"missing {WRAPPER}"
    assert os.access(WRAPPER, os.X_OK), "wrapper must be chmod +x"


# --------------------------------------------------------------------------
# Allowed: the two commands the lane is made of
# --------------------------------------------------------------------------


def test_canonical_pull_is_allowed_and_config_is_pinned(hub):
    proc = run(PULL_OK, hub)
    assert proc.returncode == 0, proc.stderr
    argv = argv_of(proc)
    assert argv == [
        str(hub["bin"]),
        "--config",
        str(hub["config"]),
        "pull",
        "--agent",
        "frontsteps-worker",
        "--node",
        "frontsteps",
        "--max",
        "3",
    ]


@pytest.mark.parametrize(
    "command",
    [
        "agentco pull --agent frontsteps-worker",
        "agentco pull -a frontsteps-worker --node frontsteps",
        "agentco pull --agent frontsteps-worker --reconcile",
        "agentco pull --agent frontsteps-worker --ttl 900 --max 1",
        "agentco pull --agent frontsteps-worker --reconcile-after 6.5",
    ],
)
def test_pull_forms_allowed(command, hub):
    proc = run(command, hub)
    assert proc.returncode == 0, proc.stderr
    assert argv_of(proc)[3] == "pull"


@pytest.mark.parametrize(
    "command",
    [
        REPORT_OK,
        "agentco report ac-b8700758 --attempt 2 --done",
        "agentco report ac-b8700758 --attempt 1 --done --idempotency-key frontsteps-worker:ac-b8700758:1",
        "agentco report ac-b8700758 --attempt 1 --failed --result no-executor-wired-yet",
    ],
)
def test_report_forms_allowed(command, hub):
    proc = run(command, hub)
    assert proc.returncode == 0, proc.stderr
    assert argv_of(proc)[3] == "report"


def test_quoted_result_becomes_one_argument(hub):
    """The wrapper reassembles a quoted value itself — no shell ever sees it."""
    proc = run(
        "agentco report ac-b8700758 --attempt 1 --failed --result 'no executor wired yet'",
        hub,
    )
    assert proc.returncode == 0, proc.stderr
    argv = argv_of(proc)
    assert argv[-1] == "no executor wired yet"
    assert argv[-2] == "--result"


def test_single_token_quoted_result(hub):
    proc = run("agentco report ac-1 --attempt 1 --done --result 'landed'", hub)
    assert proc.returncode == 0, proc.stderr
    assert argv_of(proc)[-1] == "landed"


# --------------------------------------------------------------------------
# Denied: everything else
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command,why",
    [
        ("rm -rf /", "the whole reason forced commands exist"),
        ("rm -rf ~/Portfolio", "destructive path form"),
        ("bash", "interactive shell"),
        ("/bin/sh -c ls", "explicit interpreter"),
        ("agentco", "no subcommand"),
        ("agentco tasks list", "a real agentco command that is not on the lane"),
        ("agentco cycle", "hub orchestration is not the worker's to trigger"),
        ("agentco doctor", "read-only, still not on the lane"),
        ("cat /Users/x/.claude/.env", "credential exfiltration"),
        ("/usr/bin/agentco pull --agent w", "absolute path for argv[0]"),
        ("./agentco pull --agent w", "relative path for argv[0]"),
        ("AGENTCO_BIN=/tmp/evil agentco pull --agent w", "env prefix"),
    ],
)
def test_non_lane_commands_denied(command, why, hub):
    proc = run(command, hub)
    assert proc.returncode == DENIED, f"{why}: expected denial, got {proc.stdout}"


@pytest.mark.parametrize(
    "command",
    [
        "agentco pull --agent w; rm -rf /",
        "agentco pull --agent w && rm -rf /",
        "agentco pull --agent w | tee /tmp/x",
        "agentco pull --agent w > /tmp/x",
        "agentco pull --agent w < /etc/passwd",
        "agentco pull --agent $(whoami)",
        "agentco pull --agent `whoami`",
        "agentco pull --agent ${HOME}",
        "agentco pull --agent w & rm -rf /",
        "agentco pull --agent w\nrm -rf /",
        "agentco pull --agent w$(printf x)",
        "agentco pull --agent *",
        "agentco pull --agent w\\; rm -rf /",
        'agentco pull --agent "w"',
    ],
)
def test_shell_metacharacters_denied(command, hub):
    """Denied at the charset gate, before any parsing — one assertion instead of
    a per-metacharacter argument about whether the parser survived it."""
    proc = run(command, hub)
    assert proc.returncode == DENIED, f"expected denial for {command!r}"
    assert "illegal characters" in proc.stderr or "denied" in proc.stderr


def test_empty_command_denied(hub):
    """An unset SSH_ORIGINAL_COMMAND is `ssh bigmac` with no command — a login."""
    proc = run(None, hub)
    assert proc.returncode == DENIED
    assert "interactive shell" in proc.stderr


def test_config_override_denied(hub, tmp_path):
    """A remote key must not be able to point the hub at another store."""
    other = tmp_path / "other.yaml"
    other.write_text("tasks_path: tasks.jsonl\n")
    for form in (f"agentco pull --agent w --config {other}", f"agentco pull --agent w -c {other}"):
        proc = run(form, hub)
        assert proc.returncode == DENIED, form


def test_force_denied(hub):
    """`--force` overrides reconcile-before-replay. A guard the guarded party can
    switch off is not a guard; break-glass is a human act on bigmac."""
    proc = run("agentco pull --agent frontsteps-worker --force", hub)
    assert proc.returncode == DENIED
    assert "--force" in proc.stderr


@pytest.mark.parametrize(
    "command",
    [
        "agentco pull --agent=frontsteps-worker",
        "agentco report ac-1 --attempt=1 --done",
    ],
)
def test_equals_form_denied(command, hub):
    """Only the space-separated form is parsed; the equals form is refused
    explicitly rather than half-supported."""
    assert run(command, hub).returncode == DENIED


@pytest.mark.parametrize(
    "command",
    [
        "agentco pull --agent frontsteps-worker --max abc",
        "agentco pull --agent frontsteps-worker --ttl -5",
        "agentco pull --agent frontsteps-worker --max",
        "agentco pull --agent --node frontsteps",
        "agentco pull --node frontsteps",
        "agentco pull --agent ../../etc/passwd",
        "agentco pull --agent /etc/passwd",
        "agentco pull --agent frontsteps-worker --unknown-flag x",
        "agentco pull --agent frontsteps-worker extra-positional",
    ],
)
def test_malformed_pull_denied(command, hub):
    assert run(command, hub).returncode == DENIED, command


@pytest.mark.parametrize(
    "command",
    [
        "agentco report ac-1 --done",
        "agentco report --attempt 1 --done",
        "agentco report ../ac-1 --attempt 1 --done",
        "agentco report ac-1 --attempt one --done",
        "agentco report ac-1 --attempt 1 --done --result 'unterminated",
        "agentco report ac-1 --attempt 1 --done --quiet",
        "agentco report ac-1 --attempt 1 --done --idempotency-key ../x",
    ],
)
def test_malformed_report_denied(command, hub):
    assert run(command, hub).returncode == DENIED, command


def test_oversized_result_denied(hub):
    proc = run(
        "agentco report ac-1 --attempt 1 --failed --result " + "x" * 600, hub
    )
    assert proc.returncode == DENIED
    assert "too long" in proc.stderr


# --------------------------------------------------------------------------
# Audit: a lane that executes without leaving a record is not auditable
# --------------------------------------------------------------------------


def test_allowed_invocation_is_logged(hub):
    run(PULL_OK, hub)
    lines = audit_lines(hub)
    assert len(lines) == 1
    assert " ALLOW " in lines[0]
    assert "from=10.0.0.9" in lines[0]
    assert PULL_OK in lines[0]


def test_denied_invocation_is_logged_with_reason(hub):
    run("rm -rf /", hub)
    lines = audit_lines(hub)
    assert len(lines) == 1
    assert " DENY " in lines[0]
    assert "rm -rf /" in lines[0]


@pytest.mark.parametrize(
    "command",
    [
        "agentco pull --agent w\nrm -rf /",
        "agentco pull --agent frontsteps-worker\n",
        "agentco pull --agent frontsteps-worker\r\nrm -rf /",
    ],
)
def test_newline_smuggling_denied(command, hub):
    """Regression. Two independent holes met here: `read -r -a` consumes one
    line (so line two was dropped unexamined) and `$(...)` strips a trailing
    newline (so the charset gate saw nothing wrong). Either alone let a
    multi-line request through as a clean single-line one."""
    proc = run(command, hub)
    assert proc.returncode == DENIED, proc.stdout
    assert "newline" in proc.stderr


def test_audit_record_stays_one_line(hub):
    """A newline in the request must not forge a second audit record."""
    run("agentco pull --agent w\nrm -rf /", hub)
    lines = audit_lines(hub)
    assert len(lines) == 1
    assert " DENY " in lines[0]
    assert "rm -rf /" in lines[0]  # flattened into the same record, not hidden


def test_audit_log_appends(hub):
    run(PULL_OK, hub)
    run("rm -rf /", hub)
    run(REPORT_OK, hub)
    lines = audit_lines(hub)
    assert len(lines) == 3
    assert [ln.split()[1] for ln in lines] == ["ALLOW", "DENY", "ALLOW"]


def test_unwritable_audit_log_fails_closed(hub, tmp_path):
    """Fail closed: an unauditable remote-execution path is worse than a stalled
    lane, and a silently unlogged one is worst."""
    blocked = tmp_path / "nolog"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        proc = run(PULL_OK, hub, AGENTCO_PULL_AUDIT_LOG=str(blocked / "audit.log"))
        assert proc.returncode == 78, proc.stdout
        assert "cannot write audit log" in proc.stderr
    finally:
        blocked.chmod(0o700)


def test_missing_binary_fails_closed_not_open(hub):
    """Without dry-run the wrapper must refuse a missing binary, not exec
    whatever `agentco` happens to be on PATH."""
    proc = run(PULL_OK, hub, dry_run=False)
    assert proc.returncode == 78
    assert "no agentco at" in proc.stderr


# --------------------------------------------------------------------------
# The worker's decision logic, exercised against a stub hub (no SSH)
# --------------------------------------------------------------------------

jq_required = pytest.mark.skipif(
    shutil.which("jq") is None, reason="worker parses hub JSON with jq"
)


