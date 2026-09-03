"""Claude subagent execution — a subprocess boundary, not an LLM client.

Two execution modes:

  run_claude_task(prompt)         — full prompt via stdin, result from stdout
  run_store_backed_task(task_id)  — tiny prompt (task ID only); agent reads
                                    context from AgentCo store and writes
                                    TaskResult back via `agentco tasks complete`

Store-backed mode is the standard path for group-chat and Obsidian flows:
context lives in the task, the reply/note lives in task.result — no size
limits, no truncation anxiety, failures are inspectable mid-run.

Budget defaults: 10 minutes, 50 turns. Overridable per recurring definition
via `budget: {timeout, max_turns}` (copied into the spawned bead's metadata).
Timeout or turn exhaustion fails the bead loudly, never hangs the cycle.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import usage

DEFAULT_TIMEOUT = 600  # seconds — 10 minutes
DEFAULT_MAX_TURNS = 50

# --- stalled-builder watchdog + completion marker ---------------------------
#
# Two independent mechanisms for the same failure: a worked agent that neither
# finishes nor exits. Neither depends on the executing process behaving.
#
#   IDLE TIMEOUT  — no sign of life for N seconds → terminate the child and
#                   FAIL the bead. Not verify_failed: the work never finished,
#                   so there is nothing to verify.
#
#                   "Sign of life" is deliberately NOT just a byte on stdout or
#                   stderr. `_base_cmd` runs the CLI with `--print
#                   --output-format json`, which buffers ALL output until the
#                   process exits — measured on a real child: zero stdout bytes
#                   for 34.2s of a 35.7s run. Treating stream silence as a
#                   stall therefore turned this watchdog into a hard wall-clock
#                   cap that silently overrode the configured budget, and it
#                   killed two consecutive @aidotengineer ingest beads at
#                   exactly 900.0s while they were mid-tool-call (RCA
#                   ac-d82a660f, ac-3bb9581f; beads ac-4f095dcf, ac-1be082d5).
#                   The child's own session transcript IS appended throughout
#                   the run, so it supplies the liveness signal the streams
#                   cannot — see _TranscriptProbe.
#   DONE MARKER   — the agent is instructed to end its final message with
#                   `AGENTCO_DONE: <one-line result>`. Its ABSENCE on a clean
#                   exit is recorded (metadata.completion_marker = "missing")
#                   and warned about, never failed — v1 measures before it
#                   enforces.
COMPLETION_MARKER = "AGENTCO_DONE:"

# Deliberately not anchored to a line start: on the `--output-format json` path
# the marker arrives inside a JSON string field, so the "line" is an escaped
# `\n` rather than a real one. The last match wins — an agent that quotes the
# instruction back before emitting its real marker must not fool the check.
_MARKER_RE = re.compile(re.escape(COMPLETION_MARKER) + r"[ \t]*([^\r\n]*)")

# How long a terminated child is given to die politely before SIGKILL.
_TERMINATE_GRACE_S = 5

# How long the stream pumps get to drain after the child is gone. Bounded and
# short on purpose: a KILLED agent can leave a grandchild (its own tool
# subprocess) holding the write end of the pipe, so the pumps see no EOF at
# all. Waiting on them would turn every stall kill into a second, longer stall.
_DRAIN_GRACE_S = 1.0

# Per-bead context injection caps (metadata.context_refs). Small on purpose:
# these are POINTERS the plan pinned, not an attempt to move the repo into the
# prompt — the agent has file tools and is told where the rest is.
CONTEXT_REF_FILE_CAP = 2048
CONTEXT_REF_TOTAL_CAP = 8192

_COMPLETION_MARKER_RULE = (
    f"- End your FINAL message with EXACTLY this line and nothing after it:\n"
    f"    {COMPLETION_MARKER} <one-line summary of the result>\n"
    f"  The runtime detects that line to confirm you finished rather than stalled.\n"
)

# Keys that must be stripped before spawning a Claude (OAuth) subagent.
# CLAUDECODE blocks nested sessions; ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN
# override OAuth and can route billing to a different account.
_STRIP_KEYS = {"CLAUDECODE", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}

# z.ai Coding Plan endpoint — Anthropic-compatible API, no OAuth needed.
# Claude Code reads ANTHROPIC_BASE_URL and strips /v1 before appending its own path.
# The Coding Plan endpoint is the Anthropic-compat base; Claude Code adds /v1/messages.
_ZAI_BASE_URL = "https://api.z.ai/api/anthropic"

# z.ai's Anthropic-compat endpoint authenticates with a Bearer token, i.e. Claude Code's
# ANTHROPIC_AUTH_TOKEN (NOT ANTHROPIC_API_KEY, which sends x-api-key and z.ai rejects).
# Verified 2026-07-14: `Authorization: Bearer <key>` → 200; strip API_KEY so it can't win.
_ZAI_STRIP_KEYS = {"CLAUDECODE", "ANTHROPIC_API_KEY"}

# z.ai serves GLM models; Claude Code otherwise requests claude-* names against the
# z.ai base URL. Map the three CLI tiers to GLM so a model=None subagent resolves to GLM.
_ZAI_MODEL_ENV = {
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-4.7",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-4.7",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-4.6",
}
# The claude CLI (2.x) ignores ANTHROPIC_DEFAULT_*_MODEL and requests its built-in
# claude-* default when --model is absent, which z.ai rejects ("Unknown Model").
# So the z.ai path must pass an explicit GLM model. A claude-* model (e.g. from a
# tier that resolves to an Anthropic name) is remapped to the GLM default here —
# evidence-based zai-by-tier routing is the deferred ModelRoutingEval concern.
_ZAI_DEFAULT_MODEL = "glm-4.7"


def _zai_model(model: str | None) -> str:
    """Map any requested model to a GLM name z.ai will accept.

    A GLM name passes through. A cheap-tier alias maps to the cheap GLM; every
    other name (claude-*, the sonnet/opus aliases, None) maps to the capable
    default. This keeps feeds `ingest_model: haiku` and tier aliases from
    reaching z.ai verbatim (which would 400 with 'Unknown Model').
    """
    if model and model.startswith("glm"):
        return model
    if model in {"haiku", "claude-haiku-4-5"}:
        return "glm-4.6"
    return _ZAI_DEFAULT_MODEL

_TASK_RESULT_SCHEMA = """\
{
  "status": "complete" | "partial" | "needs_input" | "failed",
  "output": "<main deliverable text>",
  "reply": "<pre-formed Telegram/group-chat reply, or null>",
  "obsidian_note": "<Obsidian note path if you saved content there, or null>",
  "continuation_hint": "<what remains if status is partial, or null>",
  "error": "<error description if status is failed, or null>"
}"""


@dataclass
class ExecResult:
    success: bool
    output: str
    error: str | None
    exit_code: int | None
    duration_seconds: float
    truncated: bool = False  # True if stop_reason == max_tokens
    # --- telemetry lifted from the CLI's JSON envelope ---------------------
    # The envelope already carried these; the parser read stop_reason and threw
    # the rest away. Without them there is no cost-per-completed-bead, and
    # without THAT every model-routing decision stays a judgment call — which
    # is exactly what Plans/ModelRoutingEval.md was written to end.
    # All optional: a non-JSON or older envelope simply leaves them None.
    cost_usd: float | None = None
    model_used: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    num_turns: int | None = None
    # Cache tokens are billed differently from fresh input and are the single
    # biggest lever on a long-running agent's real cost, so they are lifted
    # separately rather than folded into input_tokens. None means "this route
    # did not report it" — never 0, which would be a claim that no cache was hit.
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    # --- stall detection ---------------------------------------------------
    # `completion_marker` is the one-line result the agent emitted after
    # AGENTCO_DONE:, or None when it never emitted one. `idle_timeout_hit`
    # marks a run the watchdog killed, so the caller can distinguish "stalled"
    # from "crashed" without parsing the error string.
    completion_marker: str | None = None
    idle_timeout_hit: bool = False


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _STRIP_KEYS}


# Unattended-billing opt-in (2026-08-25, sandbox-scaleout step 1): when
# ANTHROPIC_API_KEY_UNATTENDED exists — process env first, then the canonical
# ~/.claude/.env — every headless claude subprocess this module spawns bills
# against that API key (spend-limited) instead of the interactive OAuth
# session and its shared 5-hour window. Absence = exact prior behavior.
# Deliberately NOT named ANTHROPIC_API_KEY so wrappers that `source ~/.claude/.env`
# wholesale can never hand the key to an interactive session by accident.
_UNATTENDED_KEY_NAME = "ANTHROPIC_API_KEY_UNATTENDED"
_CANONICAL_ENV_FILE = Path.home() / ".claude" / ".env"


def _unattended_api_key(env_file: Path | None = None) -> str | None:
    """The unattended-billing API key, or None (= inherit the login session)."""
    key = os.environ.get(_UNATTENDED_KEY_NAME)
    if key:
        return key
    try:
        text = (env_file or _CANONICAL_ENV_FILE).read_text()
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(_UNATTENDED_KEY_NAME + "="):
            val = line.split("=", 1)[1].strip().strip('"').strip("'")
            return val or None
    return None


def _claude_env() -> dict[str, str]:
    """Env for a headless claude subprocess: cleaned, plus the unattended
    billing key when the principal has provisioned one (see above)."""
    env = _clean_env()
    key = _unattended_api_key()
    if key:
        env["ANTHROPIC_API_KEY"] = key
    return env


def _zai_env(api_key: str | None = None) -> dict[str, str]:
    """Build env for a z.ai-backed subagent.

    Strips CLAUDECODE (no nesting) and ANTHROPIC_API_KEY (avoid confusion),
    then injects ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN pointing at z.ai's
    Anthropic-compatible Coding Plan endpoint. Key resolution:
      explicit api_key arg → ZAI_API_KEY env → raises loudly.
    """
    key = api_key or os.environ.get("ZAI_API_KEY")
    if not key:
        raise ValueError(
            "z.ai API key not found — set ZAI_API_KEY in env or llm.zai_api_key in config"
        )
    # Strip CLAUDECODE (no nesting) and ANTHROPIC_API_KEY (x-api-key must not win over
    # the Bearer token). Set ANTHROPIC_AUTH_TOKEN (Bearer) + ANTHROPIC_BASE_URL, and map
    # the CLI model tiers to GLM so a model=None subagent doesn't request a claude-* name.
    env = {k: v for k, v in os.environ.items() if k not in _ZAI_STRIP_KEYS}
    env["ANTHROPIC_AUTH_TOKEN"] = key
    env["ANTHROPIC_BASE_URL"] = _ZAI_BASE_URL
    env.update(_ZAI_MODEL_ENV)
    return env



# Bare non-zero exit with empty stderr is the CLI's signature for a transient
# crash (API overload, subprocess killed before it could log) rather than a
# real logic/tool failure — those normally produce stderr. One bounded retry
# absorbs the transient case without masking a genuine repeatable failure.
_BARE_EXIT_RETRY_BACKOFF_S = 5

# Upstream statuses the API itself declares temporary. These are the OPPOSITE
# signature to a bare exit: the CLI knows exactly what went wrong and says so on
# stdout ("api_error_status=529 | API Error: 529 Overloaded ... try again in a
# moment"). That detail used to DISQUALIFY the retry above, because eligibility
# was keyed on the absence of a diagnosis rather than on what the diagnosis
# said — so an uninformative crash got a second chance while an explicitly
# temporary server overload got none, failed the bead, and spawned a full-price
# RCA for a failure whose own error text says to retry (ac-3df8e12a,
# 2026-08-18). Retryability is a property of the STATUS, not of how much the CLI
# managed to tell us. Bounded to two backoffs so a real outage still fails loudly.
_RETRYABLE_API_STATUSES = frozenset({429, 500, 502, 503, 504, 529})
_API_RETRY_BACKOFFS_S = (20, 60)

# The claude CLI writes advisory warnings to stderr on EVERY run, including
# successful ones — e.g. the connectors notice emitted whenever an auth source
# is set, which the z.ai path sets deliberately (ISC-15). Treating stderr as the
# failure reason therefore reports a constant warning as the cause of a failure.
# That is not a cosmetic problem: five ingest beads recorded "claude.ai
# connectors are disabled" when the truth was an HTTP 429 from z.ai, and the
# RCA loop then spent Opus analyzing the wrong thing. Warnings are stripped so
# the real cause can surface; if warnings are ALL there is, we say so plainly
# rather than passing one off as a diagnosis.
_WARNING_LINE_PREFIXES = ("⚠", "Warning:", "warning:")


def _meaningful_stderr(stderr: str) -> str:
    """stderr with advisory warning lines removed."""
    kept = [
        line for line in stderr.splitlines()
        if line.strip() and not line.strip().startswith(_WARNING_LINE_PREFIXES)
    ]
    return "\n".join(kept).strip()


def _stdout_failure_detail(stdout: str) -> str:
    """The real failure cause out of `--output-format json` stdout, if present.

    The CLI reports API-level failures (rate limits, quota, upstream errors) in
    its JSON result on stdout, NOT on stderr. A failure report that reads only
    stderr is therefore structurally blind to the most common real cause.
    """
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, AttributeError, TypeError):
        # Not JSON (a crash before the CLI could format output). The raw tail is
        # still better evidence than nothing — bounded so a huge dump can't
        # swamp the bead record.
        tail = (stdout or "").strip()
        return f"stdout tail: {tail[-400:]}" if tail else ""
    if not isinstance(data, dict):
        return ""
    parts = []
    if status := data.get("api_error_status"):
        parts.append(f"api_error_status={status}")
    if (subtype := data.get("subtype")) and subtype != "success":
        parts.append(f"subtype={subtype}")
    if data.get("is_error") and (result := data.get("result")):
        parts.append(str(result)[:400])
    elif not parts and (result := data.get("result")):
        parts.append(str(result)[:400])
    return " | ".join(parts)


def _retryable_api_status(stdout: str) -> int | None:
    """The upstream HTTP status from a `--output-format json` envelope, when it
    names a transient server-side condition worth one more attempt.

    Returns None for anything else — a 4xx that is an answer (400, 401, 403), a
    non-numeric field, or output that is not the JSON envelope at all.
    """
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        status = int(data.get("api_error_status"))
    except (TypeError, ValueError):
        return None
    return status if status in _RETRYABLE_API_STATUSES else None


def extract_result_text(stdout: str) -> str:
    """The human-readable text out of a `--output-format json` envelope.

    ``ExecResult.output`` on the ``run_claude_task`` path is the RAW stdout —
    the whole JSON envelope, not the agent's answer — because the executor's
    other callers (store-backed tasks) never read ``.output`` at all; they
    read the bead's own ``result``. A caller that DOES want the CLI's prose
    answer (chat replies) has always had to parse this itself, and one path
    (webui) simply didn't, storing the raw blob straight into the thread.
    This is the one place that extraction happens now.

    Success envelope: ``result`` holds the answer verbatim. Error envelope
    (``is_error`` set, or a non-success ``subtype``): reuse
    ``_stdout_failure_detail`` so a failed run's stored text names the real
    cause instead of an empty string or an unreadable blob. Non-JSON stdout
    (a crash before the CLI could format output) falls back to the raw text.
    """
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return (stdout or "").strip()
    if not isinstance(data, dict):
        return (stdout or "").strip()
    result = data.get("result")
    if isinstance(result, str) and result.strip() and not data.get("is_error"):
        return result.strip()
    detail = _stdout_failure_detail(stdout)
    if detail:
        return detail
    if isinstance(result, str) and result.strip():
        return result.strip()
    return (stdout or "").strip()


def _compose_failure(label: str, returncode: int, stdout: str, stderr: str) -> str:
    """Build a failure message that names the actual cause where one exists."""
    detail = _stdout_failure_detail(stdout)
    real_stderr = _meaningful_stderr(stderr)
    causes = [c for c in (detail, real_stderr) if c]
    if causes:
        return f"{label} subagent exited {returncode}: " + " || ".join(causes)
    if stderr.strip():
        # Warnings only — say that explicitly instead of quoting one as a cause.
        return (
            f"{label} subagent exited {returncode} with no error output "
            f"(stderr held only advisory warnings; cause not reported by the CLI)"
        )
    return f"{label} subagent exited {returncode}: (no stderr, retried once, still failed)"


@dataclass
class _ProcOutcome:
    """What a supervised subprocess did. Mirrors CompletedProcess + a stall flag."""

    returncode: int
    stdout: str
    stderr: str
    idle_killed: bool


def _transcript_root(env: dict[str, str]) -> Path:
    """Where the CLI writes per-session transcripts, for the env we spawn with.

    `CLAUDE_CONFIG_DIR` wins when the child is pointed at an isolated config
    dir; otherwise the CLI's default `~/.claude`.
    """
    base = env.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return Path(base) / "projects"


def _find_transcript(root: Path, session_id: str) -> Path | None:
    """The child's transcript file, located by its pinned session id.

    Globbed rather than derived: the CLI names the project directory from the
    slugified REALPATH of the child's cwd, so re-deriving that rule here would
    already be wrong for a symlinked cwd (`/tmp` → `/private/tmp` on macOS) and
    would break again on any future change to the slug format. The session id
    is ours — we passed it — so matching on the filename is exact and needs to
    know nothing about the directory layout.
    """
    try:
        return next(iter(root.glob(f"*/{session_id}.jsonl")), None)
    except OSError:
        return None


class _TranscriptProbe:
    """Liveness read off the child's own session transcript.

    Exists because stdout is useless as a liveness signal on this path: the CLI
    runs with `--output-format json` and emits nothing until exit. The
    transcript is appended per message and per tool result for the whole run —
    measured on a real child, it was touched every 0.2–5.1s across a run whose
    stdout stayed at zero bytes until the final second.

    `idle_seconds()` distinguishes "quiet" from "unknown" on purpose. A missing
    transcript is NO EVIDENCE, not evidence of a stall, and the caller must not
    read it as one.
    """

    def __init__(self, root: Path, session_id: str) -> None:
        self._root = root
        self._session_id = session_id
        self._path: Path | None = None

    def idle_seconds(self) -> float | None:
        """Seconds since the transcript was last written, or None if there is
        no transcript to read."""
        if self._path is None:
            self._path = _find_transcript(self._root, self._session_id)
        if self._path is None:
            return None
        try:
            return max(0.0, time.time() - self._path.stat().st_mtime)
        except OSError:  # deleted or unreadable under us — back to no evidence
            self._path = None
            return None


def _terminate(proc: subprocess.Popen) -> None:
    """SIGTERM, then SIGKILL if the child ignores it. Never raises."""
    for stop in (proc.terminate, proc.kill):
        if proc.poll() is not None:
            return
        try:
            stop()
        except (ProcessLookupError, OSError):
            return
        try:
            proc.wait(timeout=_TERMINATE_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            continue


def _supervise(
    cmd: list[str],
    prompt: str,
    timeout: int,
    env: dict[str, str],
    idle_timeout_s: int,
    idle_probe: _TranscriptProbe | None = None,
) -> _ProcOutcome:
    """Run `cmd`, feeding `prompt` on stdin, watching for a stall.

    Same contract as ``subprocess.run(..., input=prompt, capture_output=True,
    text=True, timeout=timeout)`` — including raising ``TimeoutExpired`` with
    the partial stdout attached — plus one addition: when ``idle_timeout_s`` is
    non-zero and the child shows no sign of life for that long, it is
    terminated and the outcome comes back with ``idle_killed=True``.

    No busy-polling anywhere. Three daemon threads (stdin feeder, two stream
    pumps) and a fourth that blocks in ``proc.wait()``; the supervising thread
    sleeps on an Event with a timeout equal to the remaining idle window, so it
    wakes once per idle window rather than spinning. Every byte read stamps a
    monotonic last-activity time.

    Stream bytes alone are NOT sufficient evidence of a stall, because the
    store-backed path buffers all output until exit (see the module header).
    ``idle_probe`` supplies the second, authoritative signal: only when the
    streams have been quiet for the full window is the probe consulted, and the
    child is killed only if the probe AGREES it has been quiet that long. A
    probe that cannot say — no transcript to read — fails OPEN: the run
    continues under the whole-run ``timeout``, which is the budget that was
    actually configured. That asymmetry is deliberate. A false kill destroys a
    cycle of real work and has happened twice; a false survival costs at most
    the remaining budget and is still bounded.

    stdin is fed from its own thread because a prompt larger than the pipe
    buffer would otherwise block the supervisor forever against a child that
    never reads (the exact stall this function exists to catch).
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    lock = threading.Lock()
    chunks: dict[str, list[str]] = {"stdout": [], "stderr": []}
    last_activity = time.monotonic()
    finished = threading.Event()

    def pump(stream, key: str) -> None:
        nonlocal last_activity
        try:
            for line in iter(stream.readline, ""):
                with lock:
                    last_activity = time.monotonic()
                    chunks[key].append(line)
        except (ValueError, OSError):  # stream closed under us by _terminate
            pass
        finally:
            try:
                stream.close()
            except (ValueError, OSError):
                pass

    def feed() -> None:
        try:
            if proc.stdin is not None:
                proc.stdin.write(prompt)
                proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass  # the child exited before reading its prompt — its exit says so
        finally:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except (BrokenPipeError, ValueError, OSError):
                pass

    def reap() -> None:
        try:
            proc.wait()
        finally:
            finished.set()

    workers = [
        threading.Thread(target=pump, args=(proc.stdout, "stdout"), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, "stderr"), daemon=True),
        threading.Thread(target=feed, daemon=True),
        threading.Thread(target=reap, daemon=True),
    ]
    for worker in workers:
        worker.start()

    def collected() -> tuple[str, str]:
        with lock:
            return "".join(chunks["stdout"]), "".join(chunks["stderr"])

    deadline = time.monotonic() + timeout
    idle_killed = False
    blind_warned = False
    while True:
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            _terminate(proc)
            finished.wait(timeout=_DRAIN_GRACE_S)
            out, err = collected()
            raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err)
        wait_for = remaining
        if idle_timeout_s:
            with lock:
                idle_for = now - last_activity
            if idle_for >= idle_timeout_s and idle_probe is not None:
                # The streams have been quiet for the whole window — which on
                # this path is the NORMAL state of a perfectly healthy child.
                # Nothing is killed until the transcript agrees. Consulted only
                # here, so a chatty child never pays for the stat().
                probe_idle = idle_probe.idle_seconds()
                if probe_idle is None:
                    if not blind_warned:
                        print(
                            f"[executor] idle watchdog has no transcript to read for "
                            f"this child — holding fire and letting the {timeout}s "
                            f"budget bound the run"
                        )
                        blind_warned = True
                    idle_for = 0.0
                else:
                    idle_for = min(idle_for, probe_idle)
            idle_left = idle_timeout_s - idle_for
            if idle_left <= 0:
                _terminate(proc)
                idle_killed = True
                break
            wait_for = min(wait_for, idle_left)
        if finished.wait(timeout=max(wait_for, 0.01)):
            break

    # Let the pumps drain whatever is still buffered before we read them.
    for worker in workers[:2]:
        worker.join(timeout=_DRAIN_GRACE_S)
    finished.wait(timeout=_DRAIN_GRACE_S)
    out, err = collected()
    return _ProcOutcome(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=out,
        stderr=err,
        idle_killed=idle_killed,
    )


def _detect_completion_marker(stdout: str) -> str | None:
    """The one-line result the agent emitted after ``AGENTCO_DONE:``, or None.

    Looks inside the CLI's JSON envelope first (that is where the agent's final
    message actually lives) and falls back to the raw stream, so the same check
    works for the json-format path and a plain-text one. An empty marker line
    still counts as present — the agent DID signal completion; it just said
    nothing useful, which is a prompt problem, not a stall.
    """
    haystack = stdout or ""
    try:
        data = json.loads(haystack)
        if isinstance(data, dict) and isinstance(data.get("result"), str):
            haystack = data["result"]
    except (json.JSONDecodeError, TypeError):
        pass
    matches = _MARKER_RE.findall(haystack)
    if not matches:
        return None
    return matches[-1].strip()


#: label → vendor the tokens are actually bought from. The label names the
#: AgentCo route; the route names who bills for it, which is the axis a cost
#: review actually needs.
_ROUTE_FOR_LABEL = {"claude": "anthropic", "z.ai": "z.ai"}


def _run_proc(
    cmd: list[str],
    prompt: str,
    timeout: int,
    claude_bin: str,
    _attempt: int = 0,
    env: dict[str, str] | None = None,
    label: str = "claude",
    idle_timeout_s: int = 0,
) -> ExecResult:
    """The metered subprocess boundary — every claude/z.ai launch passes here.

    Wraps `_run_proc_inner` in `usage.meter`, so exactly one usage row is
    written per logical execution INCLUDING its internal retries: a bare-exit
    or upstream-529 retry is one attempt at one bead's work, not a second unit
    of work, and counting it twice would inflate every run count that a cost
    review divides by.

    Attribution is validated here, BEFORE the child is spawned. A dispatch path
    that never entered `usage.attributed(...)` raises `MissingAttribution`
    rather than quietly spending — failing at the layer where the failure is
    (the layer that cannot say what the spend is for).
    """
    return usage.meter(
        lambda: _run_proc_inner(
            cmd,
            prompt,
            timeout,
            claude_bin,
            _attempt=_attempt,
            env=env,
            label=label,
            idle_timeout_s=idle_timeout_s,
        ),
        executor=label,
        route=_ROUTE_FOR_LABEL.get(label, label),
    )


def _run_proc_inner(
    cmd: list[str],
    prompt: str,
    timeout: int,
    claude_bin: str,
    _attempt: int = 0,
    env: dict[str, str] | None = None,
    label: str = "claude",
    idle_timeout_s: int = 0,
) -> ExecResult:
    """Shared subprocess runner — prompt via stdin, never via -p arg.

    `env` overrides the default cleaned env (the z.ai path passes a z.ai-scoped
    env). `label` prefixes the subagent error messages ('claude' vs 'z.ai') so
    both paths share truncation detection and bare-exit retry while keeping
    distinct, path-specific error text.

    `idle_timeout_s` arms the stalled-builder watchdog (0 = disabled, the
    default for callers that pass their own whole-prompt and read stdout).
    Arming it also appends a `--session-id` to the spawned command so the
    watchdog can find the child's transcript.
    """
    if env is None:
        env = _claude_env()

    # Pin the child's session id so the watchdog can find its transcript, the
    # only liveness signal this path has. Fresh per ATTEMPT, never reused: the
    # CLI rejects a repeated id outright ("Session ID <uuid> is already in
    # use.", exit 1), so the bare-exit retry below must re-enter with the
    # pristine `cmd` and mint a new one. Only when the watchdog is armed —
    # nothing else here depends on knowing the session.
    spawn_cmd = cmd
    idle_probe: _TranscriptProbe | None = None
    if idle_timeout_s:
        session_id = str(uuid.uuid4())
        spawn_cmd = cmd + ["--session-id", session_id]
        idle_probe = _TranscriptProbe(_transcript_root(env), session_id)

    started = time.monotonic()
    try:
        proc = _supervise(spawn_cmd, prompt, timeout, env, idle_timeout_s, idle_probe)
    except FileNotFoundError:
        return ExecResult(
            success=False,
            output="",
            error=f"claude binary {claude_bin!r} not found on PATH — cannot execute task",
            exit_code=None,
            duration_seconds=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired as e:
        return ExecResult(
            success=False,
            output=(e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
            error=f"{label} subagent timed out after {timeout}s (budget exhausted)",
            exit_code=None,
            duration_seconds=time.monotonic() - started,
        )

    duration = time.monotonic() - started

    if proc.idle_killed:
        # NOT verify_failed and never retried: the work did not finish, so
        # there is nothing to verify and nothing to suggest a second run would
        # end differently. The bead fails loudly with the reason named.
        return ExecResult(
            success=False,
            output=proc.stdout,
            error=f"idle timeout after {idle_timeout_s}s — no output and no transcript activity",
            exit_code=None,
            duration_seconds=duration,
            idle_timeout_hit=True,
            completion_marker=_detect_completion_marker(proc.stdout),
        )

    if proc.returncode != 0:
        # "Bare" means no MEANINGFUL stderr: a run whose stderr holds only the
        # ever-present advisory warnings is indistinguishable from one with
        # empty stderr, and must still get the transient-crash retry. Reading
        # raw stderr here let a constant warning suppress that retry entirely.
        bare_exit = not _meaningful_stderr(proc.stderr) and not _stdout_failure_detail(proc.stdout)
        api_status = _retryable_api_status(proc.stdout)
        if bare_exit and _attempt == 0:
            print(
                f"[executor] {label} subagent exited {proc.returncode} with no stderr "
                f"(transient signature) — retrying once after {_BARE_EXIT_RETRY_BACKOFF_S}s"
            )
            time.sleep(_BARE_EXIT_RETRY_BACKOFF_S)
            # _run_proc_inner, not _run_proc: the retry is part of THIS metered
            # execution, so it must not open a second usage row.
            return _run_proc_inner(
                cmd,
                prompt,
                timeout,
                claude_bin,
                _attempt=1,
                env=env,
                label=label,
                idle_timeout_s=idle_timeout_s,
            )
        if api_status is not None and _attempt < len(_API_RETRY_BACKOFFS_S):
            backoff = _API_RETRY_BACKOFFS_S[_attempt]
            print(
                f"[executor] {label} subagent hit upstream {api_status} "
                f"(transient, attempt {_attempt + 1}/{len(_API_RETRY_BACKOFFS_S) + 1}) "
                f"— retrying after {backoff}s"
            )
            time.sleep(backoff)
            return _run_proc_inner(
                cmd,
                prompt,
                timeout,
                claude_bin,
                _attempt=_attempt + 1,
                env=env,
                label=label,
                idle_timeout_s=idle_timeout_s,
            )
        error = _compose_failure(label, proc.returncode, proc.stdout, proc.stderr)
        if api_status is not None:
            error += (
                f" (transient {api_status} persisted across "
                f"{len(_API_RETRY_BACKOFFS_S) + 1} attempts)"
            )
        return ExecResult(
            success=False,
            output=proc.stdout,
            error=error,
            exit_code=proc.returncode,
            duration_seconds=duration,
        )

    # Detect silent truncation — claude --output-format json includes stop_reason
    truncated = False
    telemetry: dict = {}
    try:
        data = json.loads(proc.stdout)
        if data.get("stop_reason") == "max_tokens":
            truncated = True
        telemetry = _parse_telemetry(data)
    except (json.JSONDecodeError, AttributeError):
        pass

    if truncated:
        return ExecResult(
            success=False,
            output=proc.stdout,
            error="response truncated at max_tokens — split task or raise turn budget",
            exit_code=0,
            duration_seconds=duration,
            truncated=True,
            **telemetry,
        )

    return ExecResult(
        success=True,
        output=proc.stdout,
        error=None,
        exit_code=0,
        duration_seconds=duration,
        completion_marker=_detect_completion_marker(proc.stdout),
        **telemetry,
    )


def _resolve_codex_bin(codex_bin: str) -> str:
    """Resolve the codex binary. Same launchd/PATH hazard as the claude binary:
    a non-login shell has neither ~/.bun/bin nor /opt/homebrew/bin."""
    if os.path.isabs(codex_bin):
        return codex_bin
    return (
        shutil.which(codex_bin)
        or os.path.expanduser(f"~/.bun/bin/{codex_bin}")
        or f"/opt/homebrew/bin/{codex_bin}"
    )


def run_forge_task(
    prompt: str,
    timeout: int = DEFAULT_TIMEOUT,
    model: str | None = None,
    codex_bin: str = "codex",
    cwd: str | None = None,
) -> ExecResult:
    """Execute a bead via OpenAI's codex CLI (the Forge persona).

    Verified headless on 2026-07-31: `codex exec` runs with no TTY under a
    stripped launchd-style env, authenticating from the persisted ChatGPT
    login. That is what makes this route dispatchable unattended, unlike
    Bellows/agy (OAuth-only, no headless path — see AGENT_ROUTE).

    FORGE is a RESTRICTED-capable route (OpenAI is one of the two vendors
    cleared for it), so egress authorization admits any bead class. The gate
    still runs: `AGENT_ROUTE` must name the route, and a future ceiling change
    in models.ts propagates here without a code change.

    Prompt goes as an argument rather than stdin — `codex exec` takes the
    prompt positionally. Long prompts are the reason the claude path uses
    stdin; if Forge beads start hitting ARG_MAX, switch to `-` + stdin.

    Metered like every other model-invoking path. `codex exec` reports no token
    counts and no price, so those columns land as NULL — which is the honest
    record: this route's spend is real and unmeasured, and a 0 would say the
    opposite.
    """
    return usage.meter(
        lambda: _run_forge_task_inner(prompt, timeout, model, codex_bin, cwd),
        executor="forge",
        route="openai-codex",
    )


def _run_forge_task_inner(
    prompt: str,
    timeout: int,
    model: str | None,
    codex_bin: str,
    cwd: str | None,
) -> ExecResult:
    cmd = [_resolve_codex_bin(codex_bin), "exec", "--skip-git-repo-check"]
    if model:
        cmd += ["--model", model]
    if cwd:
        cmd += ["--cd", cwd]
    cmd.append(prompt)

    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_clean_env(),
        )
    except subprocess.TimeoutExpired:
        return ExecResult(
            success=False,
            output="",
            error=f"codex subagent exceeded {timeout}s timeout",
            exit_code=None,
            duration_seconds=time.time() - start,
        )
    except FileNotFoundError:
        return ExecResult(
            success=False,
            output="",
            error=(
                f"codex binary not found (tried {cmd[0]}) — Forge requires the "
                f"OpenAI codex CLI on PATH; `brew install codex` then `codex login`"
            ),
            exit_code=None,
            duration_seconds=time.time() - start,
        )
    duration = time.time() - start

    if proc.returncode != 0:
        return ExecResult(
            success=False,
            output=proc.stdout,
            error=_compose_failure("forge", proc.returncode, proc.stdout, proc.stderr),
            exit_code=proc.returncode,
            duration_seconds=duration,
        )
    return ExecResult(
        success=True,
        output=proc.stdout,
        error=None,
        exit_code=0,
        duration_seconds=duration,
        model_used=model,
    )


def _parse_telemetry(data: dict) -> dict:
    """Lift cost/usage/model out of the CLI's JSON envelope.

    Tolerant by design (validation at write boundaries, tolerance at read
    boundaries): a missing or reshaped field yields None rather than failing an
    otherwise-successful execution. Telemetry must never be able to fail work.

    `model_used` is the model that actually authored the answer — the highest
    output-token entry in `modelUsage`. That mirrors LifeOS's verifyExecutedModel
    convention, which exists because a run's background passes (a cheap
    summarizer, say) also appear in modelUsage and would otherwise be mistaken
    for the executing model. We report what RAN, never what was requested.
    """
    if not isinstance(data, dict):
        return {}
    out: dict = {}

    cost = data.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        out["cost_usd"] = float(cost)

    turns = data.get("num_turns")
    if isinstance(turns, int):
        out["num_turns"] = turns

    usage_block = data.get("usage")
    if isinstance(usage_block, dict):
        for src, dst in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("cache_read_input_tokens", "cache_read_tokens"),
            ("cache_creation_input_tokens", "cache_creation_tokens"),
        ):
            if isinstance(usage_block.get(src), int) and not isinstance(
                usage_block.get(src), bool
            ):
                out[dst] = usage_block[src]

    model_usage = data.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        def _out_tokens(entry) -> int:
            if not isinstance(entry, dict):
                return 0
            v = entry.get("outputTokens", entry.get("output_tokens", 0))
            return v if isinstance(v, int) else 0
        out["model_used"] = max(model_usage, key=lambda k: _out_tokens(model_usage[k]))

    return out


def _resolve_claude_bin(claude_bin: str) -> str:
    """Resolve the claude binary to an absolute path. A shelled-out binary must NOT
    rely on the ambient PATH: launchd/cron and other non-login-shell envs don't include
    ~/.local/bin, so a bare 'claude' fails 'not found on PATH' even though it's installed.
    Order: an already-absolute path as-is → shutil.which → the known ~/.local/bin fallback."""
    if os.path.isabs(claude_bin):
        return claude_bin
    return shutil.which(claude_bin) or os.path.expanduser(f"~/.local/bin/{claude_bin}")


def _base_cmd(claude_bin: str, max_turns: int, model: str | None) -> list[str]:
    """Shared headless-claude command. `model` pins the subagent model
    (e.g. 'haiku'/'sonnet'/'opus' or a full id); None inherits the CLI default."""
    cmd = [_resolve_claude_bin(claude_bin), "--print", "--output-format", "json", "--max-turns", str(max_turns)]
    if model:
        cmd += ["--model", model]
    return cmd


def run_claude_task(
    prompt: str,
    timeout: int = DEFAULT_TIMEOUT,
    max_turns: int = DEFAULT_MAX_TURNS,
    claude_bin: str = "claude",
    model: str | None = None,
) -> ExecResult:
    """Execute one task via a headless Claude subagent.

    Prompt delivered via stdin (no ARG_MAX limit). Never raises on execution
    failure — every failure mode comes back as a loud ExecResult.
    """
    return _run_proc(_base_cmd(claude_bin, max_turns, model), prompt, timeout, claude_bin)


def _prime_block(config_path: str | Path | None) -> str:
    """The node's cached PRIME.md, prepended to a bead prompt (or "").

    Orientation the agent would otherwise rediscover per bead. Import is local
    and failure is swallowed to "" on purpose: priming is an accelerant, never
    a precondition — a node with no cache, or an unreadable one, must still
    execute its beads exactly as before.
    """
    try:
        from .prime import injection_block

        return injection_block(config_path)
    except Exception:  # noqa: BLE001 — never let context assembly kill a run
        return ""


def _resolve_idle_timeout(
    config_path: str | Path | None, override: int | None = None
) -> int:
    """Seconds of silence the watchdog tolerates for this node (0 = disabled).

    An explicit `override` always wins (that is how a caller or a test pins it).
    Otherwise the node's `executor.idle_timeout_s` decides, and a node with no
    config falls back to the shipped default. Config-read failure degrades to
    the default rather than to *disabled*: losing the watchdog silently is the
    very failure it was built to stop.
    """
    if override is not None:
        return max(0, int(override))
    try:
        from .config import DEFAULT_IDLE_TIMEOUT_S, Config

        if config_path is None:
            return DEFAULT_IDLE_TIMEOUT_S
        return Config.load(config_path).executor.idle_timeout_s
    except Exception:  # noqa: BLE001 — a bad config must not disarm the watchdog
        from .config import DEFAULT_IDLE_TIMEOUT_S

        return DEFAULT_IDLE_TIMEOUT_S


def _context_refs_block(task_id: str, config_path: str | Path | None) -> str:
    """The bead's own pinned files, inlined under PRIME (or "").

    PRIME orients an agent in the NODE; `metadata.context_refs` pins the two or
    three files THIS bead needs, decided when the plan was written by whoever
    had the whole picture. Injecting them removes the search step that a bead
    prompt otherwise pays for on every run.

    Bounded on purpose: `CONTEXT_REF_FILE_CAP` per file, `CONTEXT_REF_TOTAL_CAP`
    overall, head-truncated with the truncation stated in the text. An excerpt
    that silently ends mid-file teaches the agent something false about the
    file; one that says where it stopped sends it to disk for the rest.

    Missing files are NOTED, not skipped silently: "the plan expected this and
    it is not there" is information, and the plan may simply be describing a
    file this bead is about to create.

    Failure is swallowed to "" like PRIME: context assembly is an accelerant,
    never a precondition.
    """
    try:
        from .beads import Beads, resolve_context_ref
        from .config import Config

        config = Config.load(config_path) if config_path else Config()
        base_dir = Path(config.tasks_path).parent
        task = Beads(config.tasks_path).get(task_id)
        refs = ((task.metadata if task else None) or {}).get("context_refs") or []
        if not refs:
            return ""

        lines = [
            "--- BEAD CONTEXT (metadata.context_refs) ---",
            "Files pinned to this bead at plan time. Excerpts only — read the "
            "file from disk when you need more than what is shown.",
            "",
        ]
        budget = CONTEXT_REF_TOTAL_CAP
        for ref in refs:
            path = str(ref.get("path", ""))
            why = str(ref.get("why", ""))
            lines.append(f"## {path} — {why}")
            if budget <= 0:
                lines.append(
                    f"(omitted: the {CONTEXT_REF_TOTAL_CAP}-char context budget "
                    f"was already spent — read this file from disk)"
                )
                lines.append("")
                continue
            resolved = resolve_context_ref(path, base_dir)
            try:
                text = resolved.read_text()
            except (OSError, UnicodeDecodeError) as e:
                lines.append(f"(not readable at {resolved}: {e} — skipped)")
                lines.append("")
                continue
            cap = min(CONTEXT_REF_FILE_CAP, budget)
            excerpt = text[:cap]
            budget -= len(excerpt)
            lines.append("```")
            lines.append(excerpt)
            lines.append("```")
            if len(excerpt) < len(text):
                lines.append(
                    f"(truncated: showing the first {len(excerpt)} of "
                    f"{len(text)} chars — read {resolved} for the rest)"
                )
            lines.append("")
        lines.append("--- END BEAD CONTEXT ---")
        lines.append("")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 — never let context assembly kill a run
        return ""


def run_store_backed_task(
    task_id: str,
    config_path: str | Path | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_turns: int = DEFAULT_MAX_TURNS,
    claude_bin: str = "claude",
    model: str | None = None,
    prompt: str | None = None,
    idle_timeout_s: int | None = None,
) -> ExecResult:
    """Store-backed execution: pass only the task ID.

    The agent reads full context from the AgentCo store and writes a
    structured TaskResult back via `agentco tasks complete --result`.
    stdout is only a completion signal — the real result is in task.result.

    Use this for group-chat flows (reply field) and Obsidian flows
    (obsidian_note field) where context grows large or continuations matter.

    `prompt` overrides the generic store-backed instruction with a caller-built
    one (the planner bead uses this to give planner-specific instructions) while
    keeping the SAME execution path — same env-strip, stdin delivery, truncation
    detection, and bare-exit retry. It is store-backed either way: the deliverable
    is read from the store, not from stdout.

    The stalled-builder watchdog is armed from the node's
    `executor.idle_timeout_s` unless `idle_timeout_s` pins it explicitly.
    """
    if prompt is None:
        config_flag = f"--config {config_path} " if config_path else ""
        prompt = (
            f"{_prime_block(config_path)}"
            f"{_context_refs_block(task_id, config_path)}"
            f"You are executing AgentCo task {task_id}.\n\n"
            f"Step 1 — read the task context:\n"
            f"  agentco {config_flag}tasks show {task_id}\n\n"
            f"Step 2 — complete the work described in the task.\n\n"
            f"Step 3 — write your result back BEFORE finishing:\n"
            f"  agentco {config_flag}tasks complete {task_id} --result '<TaskResult JSON>'\n\n"
            f"TaskResult JSON schema (all fields except status and output are optional):\n"
            f"{_TASK_RESULT_SCHEMA}\n\n"
            f"Rules:\n"
            f"- status='partial' if you ran out of time or hit a blocker; set continuation_hint.\n"
            f"- status='needs_input' if you cannot proceed without a human decision.\n"
            f"- Always write back before your final turn ends — partial results beat silence.\n"
            f"- If the task came from a Telegram group chat, populate reply with the message to send back.\n"
            f"- If you saved an Obsidian note, populate obsidian_note with the full note path.\n"
            f"{_COMPLETION_MARKER_RULE}"
        )
    return _run_proc(
        _base_cmd(claude_bin, max_turns, model),
        prompt,
        timeout,
        claude_bin,
        idle_timeout_s=_resolve_idle_timeout(config_path, idle_timeout_s),
    )


def run_zai_store_backed_task(
    task_id: str,
    config_path: str | Path | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_turns: int = DEFAULT_MAX_TURNS,
    claude_bin: str = "claude",
    model: str | None = None,
    zai_api_key: str | None = None,
    idle_timeout_s: int | None = None,
) -> ExecResult:
    """Store-backed execution via z.ai's Coding Plan (Anthropic-compat endpoint).

    Identical contract to run_store_backed_task but routes through z.ai instead
    of Anthropic OAuth. Set `agent: zai` in a recurring def to use this path.

    Key resolution: zai_api_key arg → ZAI_API_KEY env → loud ValueError.
    """
    config_flag = f"--config {config_path} " if config_path else ""
    prompt = (
        f"{_prime_block(config_path)}"
        f"{_context_refs_block(task_id, config_path)}"
        f"You are executing AgentCo task {task_id}.\n\n"
        f"Step 1 — read the task context:\n"
        f"  agentco {config_flag}tasks show {task_id}\n\n"
        f"Step 2 — complete the work described in the task.\n\n"
        f"Step 3 — write your result back BEFORE finishing:\n"
        f"  agentco {config_flag}tasks complete {task_id} --result '<TaskResult JSON>'\n\n"
        f"TaskResult JSON schema (all fields except status and output are optional):\n"
        f"{_TASK_RESULT_SCHEMA}\n\n"
        f"Rules:\n"
        f"- status='partial' if you ran out of time or hit a blocker; set continuation_hint.\n"
        f"- status='needs_input' if you cannot proceed without a human decision.\n"
        f"- Always write back before your final turn ends — partial results beat silence.\n"
        f"- If the task came from a Telegram group chat, populate reply with the message to send back.\n"
        f"- If you saved an Obsidian note, populate obsidian_note with the full note path.\n"
        f"{_COMPLETION_MARKER_RULE}"
    )
    # Route through the shared runner so truncation detection, the bare-exit
    # retry and the idle watchdog apply identically to the z.ai path. _zai_env
    # handles key resolution (raising loudly on a missing key); label='z.ai'
    # keeps the distinct prefixes.
    env = _zai_env(zai_api_key)
    cmd = _base_cmd(claude_bin, max_turns, _zai_model(model))
    return _run_proc(
        cmd,
        prompt,
        timeout,
        claude_bin,
        env=env,
        label="z.ai",
        idle_timeout_s=_resolve_idle_timeout(config_path, idle_timeout_s),
    )
