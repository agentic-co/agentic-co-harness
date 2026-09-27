"""Per-task filesystem + subprocess sandbox and the four tools an agent gets.

Each task runs in its own temp directory. The agent only ever sees files it
(or the task setup) put in THAT directory — the hidden grading test is never
written there; it lives only in `Sandbox.hidden_test_code` and is used after
the episode ends, by `grade()`, never by a tool call the agent can make.

Network: best-effort only. Subprocesses run with a minimal, secret-free
environment and proxy variables pointed at a closed local port, so most
stdlib/requests-style HTTP clients fail fast. This does NOT stop a raw socket
call — `sandbox-exec` (macOS's syscall-level sandboxer) was tried and refused
to exec even `/bin/echo` under SIP on this machine (verified 2026-09-25), and
this build had no time budget for a real namespace/VM sandbox. Documented as
a known gap in CODEBENCH.md, not silently claimed as secure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# `scripts/eval` (this package's parent) is a tool directory, not a package —
# matching `asop_agent.py`'s bootstrap, so `codebench.*` resolves the same way
# whether a module here was imported by name or brought in by path (as
# tests/conftest-style loaders sometimes do).
_PARENT = Path(__file__).resolve().parents[1]
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from codebench.datasets import Task  # noqa: E402  (path bootstrap must precede this)

MAX_FILE_BYTES = 50_000
MAX_FILES = 25
DEFAULT_TOOL_TIMEOUT_S = 10

# Poisoned so common HTTP clients that respect proxy env vars fail fast
# instead of hanging or reaching the network. Port 9 ("discard") is not
# listening on the loopback address in any normal setup.
_DEAD_PROXY = "http://127.0.0.1:9"


class SandboxViolation(Exception):
    """The agent tried something the sandbox refuses: path escape, oversize
    write, or too many files. Reported back to the agent as a tool error, not
    raised into the harness — a violation is data about the arm, not a crash."""


@dataclass
class ToolResult:
    ok: bool
    output: str  # what goes back to the model as the tool result content


class Sandbox:
    def __init__(self, task: Task, tool_timeout_s: int = DEFAULT_TOOL_TIMEOUT_S):
        self.task = task
        self.tool_timeout_s = tool_timeout_s
        self.workdir = Path(tempfile.mkdtemp(prefix="codebench_"))
        self.hidden_test_code = task.hidden_test_code
        self.original_buggy_code = task.buggy_code
        self._write_initial()

    def _write_initial(self) -> None:
        (self.workdir / "solution.py").write_text(self.task.buggy_code)
        if self.task.visible_test_code:
            (self.workdir / "visible_test.py").write_text(self.task.visible_test_code)
        for name, content in self.task.extra_files.items():
            self._safe_path(name).write_text(content)

    def cleanup(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()

    # -- path safety ----------------------------------------------------

    def _safe_path(self, rel_path: str) -> Path:
        if not rel_path or rel_path.startswith("/") or ".." in Path(rel_path).parts:
            raise SandboxViolation(f"refused path {rel_path!r}: must be a relative path with no '..'")
        p = (self.workdir / rel_path).resolve()
        if self.workdir.resolve() not in p.parents and p != self.workdir.resolve():
            raise SandboxViolation(f"refused path {rel_path!r}: escapes the sandbox")
        return p

    def _env(self) -> dict:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.workdir.as_posix(),
            "PYTHONDONTWRITEBYTECODE": "1",
            "http_proxy": _DEAD_PROXY, "https_proxy": _DEAD_PROXY, "all_proxy": _DEAD_PROXY,
            "HTTP_PROXY": _DEAD_PROXY, "HTTPS_PROXY": _DEAD_PROXY, "ALL_PROXY": _DEAD_PROXY,
        }

    # -- tools ------------------------------------------------------------

    def read_file(self, path: str) -> ToolResult:
        try:
            p = self._safe_path(path)
        except SandboxViolation as e:
            return ToolResult(False, str(e))
        if not p.exists():
            return ToolResult(False, f"no such file: {path}")
        try:
            return ToolResult(True, p.read_text())
        except UnicodeDecodeError:
            return ToolResult(False, f"{path} is not a text file")

    def write_file(self, path: str, content: str) -> ToolResult:
        try:
            p = self._safe_path(path)
        except SandboxViolation as e:
            return ToolResult(False, str(e))
        if len(content.encode()) > MAX_FILE_BYTES:
            return ToolResult(False, f"refused: {path} exceeds {MAX_FILE_BYTES} bytes")
        existing = {f for f in self.workdir.rglob("*") if f.is_file()}
        if p not in existing and len(existing) >= MAX_FILES:
            return ToolResult(False, f"refused: sandbox already has {MAX_FILES} files")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return ToolResult(True, f"wrote {path} ({len(content)} chars)")

    def run_tests(self, path: str) -> ToolResult:
        """Run one test file that's already in the sandbox (visible_test.py,
        or one the agent wrote, e.g. repro_test.py). Cannot reach the hidden
        grading test — it was never written to this directory."""
        try:
            p = self._safe_path(path)
        except SandboxViolation as e:
            return ToolResult(False, str(e))
        if not p.exists():
            return ToolResult(False, f"no such test file: {path} (write it first)")
        return self._run_script(p)

    def run_python(self, code: str) -> ToolResult:
        """Run an ad hoc snippet in a subprocess (not exec in-process — same
        isolation posture as run_tests). Not persisted as a file."""
        scratch = self.workdir / ".scratch.py"
        scratch.write_text(code)
        result = self._run_script(scratch)
        scratch.unlink(missing_ok=True)
        return result

    def _run_script(self, p: Path) -> ToolResult:
        try:
            proc = subprocess.run(
                [sys.executable, p.name],
                cwd=self.workdir,
                env=self._env(),
                capture_output=True,
                text=True,
                timeout=self.tool_timeout_s,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(False, f"timed out after {self.tool_timeout_s}s")
        out = (proc.stdout or "") + (proc.stderr or "")
        out = out[-4000:]  # clip — the model reads a verdict, not a firehose
        if proc.returncode == 0:
            return ToolResult(True, out or "(exit 0, no output)")
        return ToolResult(False, f"exit {proc.returncode}\n{out}")

    # -- grading (harness-only; never a tool call) ------------------------

    def run_hidden_test(self, solution_code: Optional[str] = None) -> ToolResult:
        """Grade a solution against the held-out test, in an ISOLATED temp
        copy — never inside the agent's own workdir, so the agent can never
        observe or influence this run."""
        with tempfile.TemporaryDirectory(prefix="codebench_grade_") as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "solution.py").write_text(
                solution_code if solution_code is not None
                else (self.workdir / "solution.py").read_text()
            )
            for name, content in self.task.extra_files.items():
                (tmp_path / name).write_text(content)
            (tmp_path / "hidden_test.py").write_text(self.hidden_test_code)
            try:
                proc = subprocess.run(
                    [sys.executable, "hidden_test.py"],
                    cwd=tmp_path, env=self._env(),
                    capture_output=True, text=True, timeout=self.tool_timeout_s,
                )
            except subprocess.TimeoutExpired:
                return ToolResult(False, f"timed out after {self.tool_timeout_s}s")
            out = ((proc.stdout or "") + (proc.stderr or ""))[-4000:]
            return ToolResult(proc.returncode == 0, out or "(exit 0, no output)")

    def run_file_against(self, test_filename: str, solution_code: str) -> ToolResult:
        """Grade one file already in the workdir (e.g. the agent's own
        `repro_test.py`) against a GIVEN solution body, in an isolated copy.
        Used for reproduction-test validity: the same repro test, run once
        against the original buggy code and once against the final fix."""
        test_path = self.workdir / test_filename
        if not test_path.exists():
            return ToolResult(False, f"no such file: {test_filename}")
        with tempfile.TemporaryDirectory(prefix="codebench_repro_") as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "solution.py").write_text(solution_code)
            (tmp_path / test_filename).write_text(test_path.read_text())
            for name, content in self.task.extra_files.items():
                (tmp_path / name).write_text(content)
            try:
                proc = subprocess.run(
                    [sys.executable, test_filename],
                    cwd=tmp_path, env=self._env(),
                    capture_output=True, text=True, timeout=self.tool_timeout_s,
                )
            except subprocess.TimeoutExpired:
                return ToolResult(False, f"timed out after {self.tool_timeout_s}s")
            out = ((proc.stdout or "") + (proc.stderr or ""))[-4000:]
            return ToolResult(proc.returncode == 0, out or "(exit 0, no output)")
