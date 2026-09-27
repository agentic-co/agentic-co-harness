#!/usr/bin/env python3
"""Materialize each `RepoSpec` at its pinned commit into
`evals/codebench/data/repobench/<name>/repo/` — a FILE snapshot, not a git
clone (no nested `.git`, no history, just the paths `RepoSpec.keep_paths`
names, at that exact commit). Re-run is safe: it re-clones to a scratch temp
dir, checks out the pinned SHA, and overwrites the snapshot.

    uv run python3 scripts/eval/codebench/repobench/snapshot.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# This file lives at scripts/eval/codebench/repobench/snapshot.py — put
# scripts/eval (parents[2]) on sys.path so `codebench.repobench.*` resolves
# the same way whether this module is imported by name or by path.
_EVAL_DIR = Path(__file__).resolve().parents[2]
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from codebench.repobench.repos import REPOS, RepoSpec  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[4]
DATA_ROOT = REPO_ROOT / "evals" / "codebench" / "data" / "repobench"


def snapshot_one(spec: RepoSpec, out_dir: Path) -> None:
    with tempfile.TemporaryDirectory(prefix=f"repobench-clone-{spec.name}-") as tmp:
        clone_dir = Path(tmp) / spec.name
        subprocess.run(
            ["git", "clone", "--quiet", f"https://github.com/{spec.github}.git", str(clone_dir)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(clone_dir), "checkout", "--quiet", spec.commit],
            check=True,
        )
        repo_out = out_dir / spec.name / "repo"
        if repo_out.exists():
            shutil.rmtree(repo_out)
        repo_out.mkdir(parents=True)
        for rel in spec.keep_paths:
            src = clone_dir / rel
            dst = repo_out / rel
            if not src.exists():
                raise FileNotFoundError(f"{spec.name}: keep_path {rel!r} not found in clone")
            if src.is_dir():
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    (out_dir / spec.name / "PROVENANCE.md").write_text(
        f"# {spec.name} — provenance\n\n"
        f"- Source: `https://github.com/{spec.github}`\n"
        f"- Commit: `{spec.commit}`\n"
        f"- Licence: {spec.license}\n"
        f"- Snapshot kept: {', '.join(spec.keep_paths)} (docs/CI/tox/bench/examples dropped —"
        " irrelevant to running the test suite locally)\n"
        f"- Test command: `python3 -m pytest {' '.join(spec.pytest_args)}` from `repo/`\n"
        f"- Baseline (verified 2026-09-26, this exact commit, this machine): {spec.baseline_summary}\n"
        f"- Mutation-eligible source dirs: {', '.join(spec.source_dirs)} "
        f"(test-dir markers excluded even when nested inside one: {', '.join(spec.test_dir_markers)})\n"
    )
    print(f"{spec.name}: snapshotted to {out_dir / spec.name / 'repo'}")


def main() -> int:
    for spec in REPOS:
        snapshot_one(spec, DATA_ROOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
