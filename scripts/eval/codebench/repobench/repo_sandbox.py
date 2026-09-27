"""Multi-file repo sandbox: a whole small pure-Python repo in a temp dir,
five tools (`list_dir` is new versus the single-file tier's `sandbox.py`),
and — the integrity safeguard that tier didn't need — TEST FILES ARE
RESTORED TO PRISTINE BEFORE GRADING. Without that, an agent could "fix" a
task by editing or deleting the failing assertion instead of the bug, and
pass@1 would never know. `run_tests` during the episode runs against
whatever the agent currently has (including any test file it wrote itself,
e.g. `repro_test.py`); only `run_hidden_test` — never a tool the agent can
call — first overwrites every test-marked path back to the snapshot's
original content, then runs the full suite.
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

_EVAL_DIR = Path(__file__).resolve().parents[2]
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from codebench.repobench.repos import RepoSpec  # noqa: E402
from codebench.sandbox import ToolResult  # noqa: E402  (reuse the same result shape)

MAX_FILE_BYTES = 300_000
MAX_FILES_CREATED = 40
MAX_WRITE_FILE_LINES_FOR_EXISTING = 200  # existing files longer than this need edit_file
DEFAULT_TOOL_TIMEOUT_S = 20  # more-itertools' own suite alone needs ~9s

_DEAD_PROXY = "http://127.0.0.1:9"

# Real, kernel-level network denial (verified 2026-09-26: a raw socket
# connect gets PermissionError, not just a proxy-env miss — see the profile
# file's own comment for what was tried and rejected first). Falls back to
# proxy-env poisoning alone (see `env()`) on a non-macOS host, or if
# `sandbox-exec` is missing — degraded, not silently absent: `network_isolation`
# reports which mode is actually active so a run can say so in its own report.
_SANDBOX_PROFILE = Path(__file__).resolve().parent / "network_deny.sb"
_SANDBOX_EXEC = shutil.which("sandbox-exec")


def network_isolation_mode() -> str:
    return "sandbox-exec" if _SANDBOX_EXEC else "proxy-env-only"


def wrap_argv(argv: list[str]) -> list[str]:
    """Public alias — `repo_agent.py`'s isolated repro-vs-solution reruns
    use the SAME wrapping this module uses internally."""
    return _wrap_argv(argv)


def _wrap_argv(argv: list[str]) -> list[str]:
    if _SANDBOX_EXEC and _SANDBOX_PROFILE.exists():
        return [_SANDBOX_EXEC, "-f", str(_SANDBOX_PROFILE), *argv]
    return argv


class SandboxViolation(Exception):
    pass


class RepoSandbox:
    def __init__(
        self,
        repo_snapshot_dir: Path,
        spec: RepoSpec,
        mutated_file: str,
        mutated_content: str,
        tool_timeout_s: int = DEFAULT_TOOL_TIMEOUT_S,
    ):
        self.spec = spec
        self.mutated_file = mutated_file
        self.tool_timeout_s = tool_timeout_s
        self._snapshot_dir = repo_snapshot_dir
        self.workdir = Path(tempfile.mkdtemp(prefix=f"repobench_{spec.name}_"))
        shutil.copytree(repo_snapshot_dir, self.workdir, dirs_exist_ok=True)
        # What the agent STARTS with (buggy) — localisation asks whether the
        # agent's FINAL content differs from THIS, not from the pre-bug
        # original (which differs from it by construction, always).
        self._buggy_content = mutated_content
        # Apply the ONE mutation on top of the pristine snapshot — the
        # snapshot itself (committed to the repo) always stays the original,
        # green source; the bug is introduced fresh per episode, here.
        (self.workdir / mutated_file).write_text(mutated_content)
        self._pristine_test_files: dict[str, str] = self._collect_test_files(repo_snapshot_dir)

    def _collect_test_files(self, snapshot_dir: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        for sd in self.spec.source_dirs:
            for py_file in (snapshot_dir / sd).rglob("*.py"):
                rel = py_file.relative_to(snapshot_dir)
                if any(part in self.spec.test_dir_markers for part in rel.parts):
                    out[str(rel)] = py_file.read_text()
        # Repos with a top-level tests/ dir outside every source_dir (more-
        # itertools, boltons) — walk the whole snapshot for anything under a
        # marker directory, not just inside source_dirs.
        for marker in self.spec.test_dir_markers:
            marker_dir = snapshot_dir / marker
            if marker_dir.is_dir():
                for py_file in marker_dir.rglob("*.py"):
                    rel = py_file.relative_to(snapshot_dir)
                    out.setdefault(str(rel), py_file.read_text())
        return out

    def cleanup(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)

    def __enter__(self) -> "RepoSandbox":
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()

    # -- path safety ------------------------------------------------------

    def _safe_path(self, rel_path: str) -> Path:
        if not rel_path or rel_path.startswith("/") or ".." in Path(rel_path).parts:
            raise SandboxViolation(f"refused path {rel_path!r}: must be relative, no '..'")
        p = (self.workdir / rel_path).resolve()
        root = self.workdir.resolve()
        if root != p and root not in p.parents:
            raise SandboxViolation(f"refused path {rel_path!r}: escapes the sandbox")
        return p

    def env(self) -> dict:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.workdir.as_posix(),
            "PYTHONDONTWRITEBYTECODE": "1",
            "http_proxy": _DEAD_PROXY, "https_proxy": _DEAD_PROXY, "all_proxy": _DEAD_PROXY,
            "HTTP_PROXY": _DEAD_PROXY, "HTTPS_PROXY": _DEAD_PROXY, "ALL_PROXY": _DEAD_PROXY,
        }

    # -- tools --------------------------------------------------------------

    def list_dir(self, path: str = ".") -> ToolResult:
        try:
            p = self._safe_path(path) if path != "." else self.workdir
        except SandboxViolation as e:
            return ToolResult(False, str(e))
        if not p.is_dir():
            return ToolResult(False, f"not a directory: {path}")
        entries = sorted(os.listdir(p))
        lines = [(e + "/") if (p / e).is_dir() else e for e in entries if not e.startswith(".")]
        return ToolResult(True, "\n".join(lines) if lines else "(empty)")

    def read_file(self, path: str) -> ToolResult:
        try:
            p = self._safe_path(path)
        except SandboxViolation as e:
            return ToolResult(False, str(e))
        if not p.exists() or not p.is_file():
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
        if p.exists() and p.is_file():
            try:
                existing_lines = p.read_text().count("\n") + 1
            except UnicodeDecodeError:
                existing_lines = 0
            if existing_lines > MAX_WRITE_FILE_LINES_FOR_EXISTING:
                # 2026-09-26 finding: a model asked to change one function in
                # a multi-thousand-line file used write_file to resend the
                # WHOLE file, which the completion's max_tokens then cut off
                # mid-file (silently — GLM's tool-calling still closed the
                # JSON cleanly). Refusing here forces edit_file for anything
                # this size, which sends only the changed lines.
                return ToolResult(False, (
                    f"refused: {path} already has {existing_lines} lines — write_file only "
                    f"creates NEW files or replaces ones with <= {MAX_WRITE_FILE_LINES_FOR_EXISTING} "
                    f"lines. Use edit_file(path, old, new) to change part of an existing file "
                    f"this size."
                ))
        if len(content.encode()) > MAX_FILE_BYTES:
            return ToolResult(False, f"refused: {path} exceeds {MAX_FILE_BYTES} bytes")
        existing = {f for f in self.workdir.rglob("*") if f.is_file()}
        if p not in existing and len(existing) >= MAX_FILES_CREATED:
            return ToolResult(False, f"refused: sandbox already has {MAX_FILES_CREATED} files")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return ToolResult(True, f"wrote {path} ({len(content)} chars)")

    def edit_file(self, path: str, old: str, new: str) -> ToolResult:
        """Exact-match string replacement — the primary way to change an
        EXISTING file, so a completion only has to generate the changed
        lines, not the whole file (see `write_file`'s size refusal above)."""
        try:
            p = self._safe_path(path)
        except SandboxViolation as e:
            return ToolResult(False, str(e))
        if not p.exists() or not p.is_file():
            return ToolResult(False, f"no such file: {path} (use write_file to create a new file)")
        if not old:
            return ToolResult(False, "refused: `old` must be a non-empty string")
        try:
            current = p.read_text()
        except UnicodeDecodeError:
            return ToolResult(False, f"{path} is not a text file")
        count = current.count(old)
        if count == 0:
            return ToolResult(False, (
                f"`old` not found in {path} — it must match EXACTLY, including whitespace and "
                f"indentation. Re-read the file and copy the exact text you want to replace."
            ))
        if count > 1:
            return ToolResult(False, (
                f"`old` appears {count} times in {path} — it must be unique. Include more "
                f"surrounding context so it matches only the one place you mean."
            ))
        updated = current.replace(old, new, 1)
        if len(updated.encode()) > MAX_FILE_BYTES:
            return ToolResult(False, f"refused: this edit would make {path} exceed {MAX_FILE_BYTES} bytes")
        p.write_text(updated)
        return ToolResult(True, f"edited {path} ({len(current)} -> {len(updated)} chars)")

    def run_tests(self, path: Optional[str] = None) -> ToolResult:
        """`path=None` runs the repo's own full suite (its `pytest_args`).
        A `path` runs just that file/nodeid — for a test the agent wrote
        itself, or an existing test it wants to re-check. Runs against
        whatever is CURRENTLY in the sandbox — including a tampered test
        file, if the agent did that; only grading (`run_hidden_test`)
        restores tests to pristine first."""
        args = list(self.spec.pytest_args)
        if path:
            try:
                p = self._safe_path(path)
            except SandboxViolation as e:
                return ToolResult(False, str(e))
            if not p.exists():
                return ToolResult(False, f"no such test path: {path} (write it first)")
            args = args + [path]
        return self._run_pytest(args)

    def run_python(self, code: str) -> ToolResult:
        scratch = self.workdir / ".scratch.py"
        scratch.write_text(code)
        result = self._run_script([sys.executable, ".scratch.py"])
        scratch.unlink(missing_ok=True)
        return result

    def _run_pytest(self, args: list[str]) -> ToolResult:
        return self._run_script([sys.executable, "-m", "pytest", *args])

    def _run_script(self, argv: list[str]) -> ToolResult:
        try:
            proc = subprocess.run(
                _wrap_argv(argv), cwd=self.workdir, env=self.env(),
                capture_output=True, text=True, timeout=self.tool_timeout_s,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(False, f"timed out after {self.tool_timeout_s}s")
        out = ((proc.stdout or "") + (proc.stderr or ""))[-6000:]
        if proc.returncode == 0:
            return ToolResult(True, out or "(exit 0, no output)")
        return ToolResult(False, f"exit {proc.returncode}\n{out}")

    # -- grading (harness-only; never a tool call) ------------------------

    def run_hidden_test(self) -> tuple[ToolResult, list[str]]:
        """Full suite, in an ISOLATED copy, with every test-marked path
        restored to the pristine snapshot first (undoes any tampering).
        Returns (result, failing_node_ids)."""
        with tempfile.TemporaryDirectory(prefix=f"repobench_grade_{self.spec.name}_") as tmp:
            tmp_path = Path(tmp)
            shutil.copytree(self.workdir, tmp_path, dirs_exist_ok=True)
            for rel, content in self._pristine_test_files.items():
                dst = tmp_path / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(content)
            try:
                proc = subprocess.run(
                    _wrap_argv([sys.executable, "-m", "pytest", *self.spec.pytest_args]),
                    cwd=tmp_path, env=self.env(),
                    capture_output=True, text=True, timeout=max(self.tool_timeout_s, 30),
                )
            except subprocess.TimeoutExpired:
                return ToolResult(False, f"timed out after {max(self.tool_timeout_s, 30)}s"), ["<timeout>"]
            out = ((proc.stdout or "") + (proc.stderr or ""))[-6000:]
            failing = [
                line.split(" ", 2)[1] for line in (proc.stdout or "").splitlines()
                if line.startswith("FAILED ")
            ]
            ok = proc.returncode == 0
            return ToolResult(ok, out or "(exit 0, no output)"), failing

    def damaged_files(self, shrink_threshold: float = 0.2) -> list[str]:
        """Source files (excluding tests) that shrank by more than
        `shrink_threshold` versus what the agent STARTED with — the buggy
        content for `mutated_file`, the pristine snapshot for everything
        else (a mutation only ever touches one file, so every other file's
        "start" and "pristine" are the same thing). 2026-09-26 finding: a
        model asked to fix one function used write_file to resend a whole
        multi-thousand-line file, which got silently truncated by the
        completion's max_tokens — this is the direct measurement of that
        failure mode, not an inference from turn count or status."""
        damaged: list[str] = []
        for py_file in self._snapshot_dir.rglob("*.py"):
            rel = py_file.relative_to(self._snapshot_dir)
            if any(part in self.spec.test_dir_markers for part in rel.parts):
                continue
            starting_content = (
                self._buggy_content if str(rel) == self.mutated_file else py_file.read_text()
            )
            starting_len = len(starting_content)
            if starting_len == 0:
                continue
            current_path = self.workdir / rel
            current_len = len(current_path.read_text()) if current_path.exists() else 0
            if current_len < starting_len * (1 - shrink_threshold):
                damaged.append(str(rel))
        return damaged

    def localisation(self) -> bool:
        """Did the agent's final code touch the file the mutation was
        actually in — i.e. does it differ from what the agent STARTED with
        (the buggy content), not from the pre-bug original (which differs
        from the buggy content by construction, always, and so would make
        this always True). Computed from a fact never exposed to any tool."""
        current = (self.workdir / self.mutated_file)
        if not current.exists():
            return False
        return current.read_text() != self._buggy_content
