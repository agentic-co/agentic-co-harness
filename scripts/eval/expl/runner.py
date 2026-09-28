"""Subprocess wrapper around `run_sopbench_asop.py`, invoked exactly the way
round 0 (the Qwen3.6 V2b run) was: same flags, same executor, same instance —
only `--asop`, `--output-dir`, and `--task-ids` vary per call. This module
does not touch that script; it only shells out to it and reads back its
output file plus a wall-clock cost record (EXP-L.md: "wall-clock/GPU/reviser-
token cost" per round).

LM Studio is assumed already loaded (the run brief: "Don't load/unload
models" — another run may be sharing the instance, which is fine at
`--parallel 1`, just slower).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
SOPBENCH_PYTHON = Path.home() / "Code" / "SOPBench" / ".venv" / "bin" / "python"
RUN_SCRIPT = REPO_ROOT / "scripts" / "eval" / "run_sopbench_asop.py"
# round 0's run.sh exports these before invoking run_sopbench_asop.py — the
# local OpenAI-compatible backend SOPBench's own OpenAIHandler needs, and
# LM Studio ignores the placeholder key. Not the caller's ambient shell env:
# a subprocess without them fails fast with "Missing credentials" instead of
# a silent misconfiguration (found live, first dry run).
DEFAULT_OPENAI_ENV = {"OPENAI_BASE_URL": "http://localhost:4242/v1", "OPENAI_API_KEY": "placeholder"}

# The exact V2b executor config every round of EXP-L runs under (EXP-L.md
# "Setting"): Qwen3.6-35B-A3B, local, --parallel 1 (set at LM Studio load
# time, not here), 2048-token cap, V2b tool-gate config (--host-rules).
EXECUTOR_MODEL = "qwen/qwen3.6-35b-a3b"
# Round 1's first attempt queued on the shared `exec-qwen36` instance per the
# original instruction; the team lead's own rerun script unloaded that
# instance mid-run (`lms unload exec-qwen36`), which silently truncated the
# run at task 48/195 ("No models loaded" from task 48 on — round 1 voided,
# see EXP-L.md's deviation log). EXP-L now loads and owns its OWN LM Studio
# identifier, `expl-qwen36`, loaded once (--parallel 1, 32768 ctx) and never
# unloaded until Stage 1 is completely done, specifically so no other
# process's lifecycle can pull the rug out from under a mid-round run again.
EXECUTOR_INSTANCE = "expl-qwen36"
DOMAIN = "hotel"


def fname_for_model(model: str) -> str:
    """Same naming rule as score_asop_arms.py's FNAME, so scoring.score_file
    finds what run_sopbench_asop.py wrote without guessing a second scheme."""
    return f"ast_{model.replace('/', '_')}-mode_fc-dep_full-fmt_structured-tool_full-shuffle_False.json"


@dataclass
class RunResult:
    ok: bool
    output_file: Optional[Path]
    log_file: Path
    returncode: int
    wall_time_s: float
    command: list[str] = field(default_factory=list)
    error: Optional[str] = None


def run_arm(
    asop_document_path: Path,
    output_dir: Path,
    task_ids_path: Optional[Path] = None,
    *,
    model: str = EXECUTOR_MODEL,
    instance: str = EXECUTOR_INSTANCE,
    domain: str = DOMAIN,
    stats_out: Optional[Path] = None,
    log_path: Optional[Path] = None,
    python: Path = SOPBENCH_PYTHON,
    extra_args: Optional[list[str]] = None,
) -> RunResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_path or (output_dir / "run.log")
    cmd = [
        str(python),
        str(RUN_SCRIPT),
        "--arm", "asop-gated",
        "--host-rules",
        "--asop", str(asop_document_path),
        "--model", model,
        "--register-model", model,
        "--instance", instance,
        "--domain", domain,
        "--assistant-max-tokens", "2048",
        "--output-dir", str(output_dir),
    ]
    if task_ids_path is not None:
        cmd += ["--task-ids", str(task_ids_path)]
    if stats_out is not None:
        cmd += ["--stats-out", str(stats_out)]
    if extra_args:
        cmd += extra_args

    env = {**os.environ, **DEFAULT_OPENAI_ENV}
    started = time.monotonic()
    with log_path.open("w") as log_fh:
        proc = subprocess.run(cmd, cwd=str(REPO_ROOT), stdout=log_fh, stderr=subprocess.STDOUT, env=env)
    wall = time.monotonic() - started

    output_file = output_dir / domain / fname_for_model(model)
    ok = proc.returncode == 0 and output_file.exists()
    return RunResult(
        ok=ok,
        output_file=output_file if output_file.exists() else None,
        log_file=log_path,
        returncode=proc.returncode,
        wall_time_s=wall,
        command=cmd,
        error=None if ok else f"exit {proc.returncode}, see {log_path}",
    )


def tail_progress(log_path: Path, n: int = 1) -> str:
    """Last `n` non-empty lines of a run log, for polling without pulling the
    whole (often multi-MB, tqdm-noisy) log into a caller's context."""
    if not log_path.exists():
        return ""
    text = log_path.read_text(errors="replace")
    lines = [ln for ln in text.replace("\r", "\n").splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def is_complete(log_path: Path, expected: int) -> bool:
    tail = tail_progress(log_path, n=5)
    return f"{expected}/{expected}" in tail
