#!/usr/bin/env python3
"""Generate the repobench mutant task set: enumerate mutation sites across
each repo's source dirs, apply candidates in a seeded deterministic order,
keep the ones the repo's OWN test suite fails on, dedupe to one kept mutant
per (file, function) so bugs spread across the codebase, derive a ticket for
each kept one, and write `evals/codebench/data/repobench/mutants.jsonl`.

    uv run python3 scripts/eval/codebench/repobench/generate_tasks.py \\
        --per-repo 20 --seed 20260926

This is dataset CONSTRUCTION, not a model run — no LLM, no network beyond
the one-time `snapshot.py` clone (already done). Every full-suite check here
runs the snapshot's own pinned test command against a throwaway temp copy;
the committed snapshot itself is never mutated in place.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

_EVAL_DIR = Path(__file__).resolve().parents[2]
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from codebench.repobench.mutate import MutationSite, apply_mutation, find_sites  # noqa: E402
from codebench.repobench.repos import REPOS, RepoSpec  # noqa: E402
from codebench.repobench.ticket import derive_ticket  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[4]
DATA_ROOT = REPO_ROOT / "evals" / "codebench" / "data" / "repobench"
PY = sys.executable


def _is_test_path(rel_path: Path, markers: list[str]) -> bool:
    return any(part in markers for part in rel_path.parts)


def _enumerate_candidates(spec: RepoSpec, repo_dir: Path) -> list[tuple[Path, MutationSite]]:
    """(relative source file, MutationSite) pairs across all of `spec`'s
    source_dirs, test files excluded even when nested inside one (toolz)."""
    out: list[tuple[Path, MutationSite]] = []
    for sd in spec.source_dirs:
        for py_file in sorted((repo_dir / sd).rglob("*.py")):
            rel = py_file.relative_to(repo_dir)
            if _is_test_path(rel, spec.test_dir_markers):
                continue
            source = py_file.read_text()
            try:
                sites = find_sites(source)
            except SyntaxError:
                continue
            for site in sites:
                out.append((rel, site))
    return out


def _run_pytest(cwd: Path, extra_args: list[str], spec: RepoSpec, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, "-m", "pytest", *spec.pytest_args, *extra_args],
        cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )


def _failed_node_ids(stdout: str) -> list[str]:
    return [line.split(" ", 2)[1] for line in stdout.splitlines() if line.startswith("FAILED ")]


def generate_for_repo(spec: RepoSpec, per_repo: int, seed: int) -> list[dict]:
    repo_snapshot = DATA_ROOT / spec.name / "repo"
    candidates = _enumerate_candidates(spec, repo_snapshot)
    rng = random.Random(f"{seed}:{spec.name}")
    order = list(range(len(candidates)))
    rng.shuffle(order)

    kept: list[dict] = []
    seen_locations: set[tuple[str, Optional[str]]] = set()
    tried = 0
    max_tried = per_repo * 25  # bound worst-case generation time, not just quota

    for idx in order:
        if len(kept) >= per_repo or tried >= max_tried:
            break
        rel_path, site = candidates[idx]
        loc_key = (str(rel_path), site.enclosing_function)
        if loc_key in seen_locations:
            continue  # dedupe: one kept mutant per (file, function)
        tried += 1

        original_source = (repo_snapshot / rel_path).read_text()
        try:
            mutated_source = apply_mutation(original_source, site)
        except ValueError:
            continue

        with tempfile.TemporaryDirectory(prefix=f"repobench-gen-{spec.name}-") as tmp:
            tmp_path = Path(tmp)
            shutil.copytree(repo_snapshot, tmp_path, dirs_exist_ok=True)
            (tmp_path / rel_path).write_text(mutated_source)
            try:
                full = _run_pytest(tmp_path, [], spec)
            except subprocess.TimeoutExpired:
                # A mutation that hangs the suite (e.g. breaks a generator's
                # exit condition into an infinite loop) is a real bug, but
                # generating a clean ticket for "it never finished" needs a
                # different code path than the assert/exception tiers below.
                # Out of scope for this build — discarded, not silently kept
                # as if it were a normal failure. See CODEBENCH.md (the "repobench" section).
                continue
            failing = _failed_node_ids(full.stdout)
            if not failing:
                continue  # equivalent mutant — behavior unchanged, discard

            node_id = failing[0]
            try:
                single = _run_pytest(tmp_path, ["--tb=long", node_id], spec)
            except subprocess.TimeoutExpired:
                continue
            ticket = derive_ticket(single.stdout, node_id,
                                    forbidden_names={site.enclosing_function or ""})

        seen_locations.add(loc_key)
        kept.append({
            "task_id": f"{spec.name}/{len(kept):03d}",
            "repo": spec.name,
            "repo_commit": spec.commit,
            "repo_license": spec.license,
            "mutated_file": str(rel_path),
            "enclosing_function": site.enclosing_function,
            "mutation_kind": site.kind,
            "mutation_detail": site.detail,
            "mutation_lineno": site.lineno,
            "ticket": ticket.text,
            "ticket_tier": ticket.tier,
            "failing_node_ids_at_generation": failing,
            "pytest_args": spec.pytest_args,
        })

    print(f"{spec.name}: kept {len(kept)}/{per_repo} "
          f"({tried} candidates tried of {len(candidates)} sites found)")
    return kept


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-repo", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20260926)
    ap.add_argument("--out", type=Path, default=DATA_ROOT / "mutants.jsonl")
    args = ap.parse_args()

    all_kept: list[dict] = []
    for spec in REPOS:
        all_kept += generate_for_repo(spec, args.per_repo, args.seed)

    with open(args.out, "w") as f:
        for row in all_kept:
            f.write(json.dumps(row) + "\n")
    print(f"total: {len(all_kept)} tasks -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
