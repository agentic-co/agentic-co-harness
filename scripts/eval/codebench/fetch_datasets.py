#!/usr/bin/env python3
"""Fetch + normalize the two codebench datasets into `evals/codebench/data/`.

    uv run --with pyarrow python3 scripts/eval/codebench/fetch_datasets.py --source humanevalfix
    python3 scripts/eval/codebench/fetch_datasets.py --source quixbugs

HumanEvalFix (Python split of bigcode/humanevalpack, HF dataset, MIT) needs
pyarrow to read the parquet file — a one-time fetch-tool dependency, not a
runtime dependency of the benchmark itself (`datasets.py` only reads the
JSONL this script writes). QuixBugs is fetched over plain HTTP (raw GitHub
files, MIT) with stdlib only.

Both writers are idempotent and record the exact upstream revision fetched in
a PROVENANCE.md next to the JSONL, so a re-run is auditable rather than
silently drifting to whatever upstream now serves.
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_ROOT = REPO_ROOT / "evals" / "codebench" / "data"

HF_PARQUET_URL = (
    "https://huggingface.co/api/datasets/bigcode/humanevalpack/parquet/python/test/0.parquet"
)
HF_API_URL = "https://huggingface.co/api/datasets/bigcode/humanevalpack"
QB_API_URL = "https://api.github.com/repos/jkoppel/QuixBugs"
QB_RAW = "https://raw.githubusercontent.com/jkoppel/QuixBugs/{sha}"

# QuixBugs ships 40 buggy programs. 9 are graph-structured (hand-written test
# functions over a shared `Node` helper, no per-case JSON fixtures) and 31 are
# flat functions graded by parametrized [input, expected] pairs in
# `json_testcases/<name>.json`. v1 takes the 31 — a fair visible/hidden split
# for the flat ones is a one-line index cut; the graph ones would need
# per-task-shape splitting logic this build didn't get to. Documented, not
# silently dropped: see evals/codebench/CODEBENCH.md "Scope decisions".
QUIXBUGS_GRAPH_BASED = frozenset({
    "breadth_first_search", "depth_first_search", "detect_cycle",
    "minimum_spanning_tree", "reverse_linked_list", "shortest_path_length",
    "shortest_path_lengths", "shortest_paths", "topological_ordering",
})
QUIXBUGS_ALL_PROGRAMS = frozenset({
    "bitcount", "breadth_first_search", "bucketsort", "depth_first_search",
    "detect_cycle", "find_first_in_sorted", "find_in_sorted", "flatten",
    "gcd", "get_factors", "hanoi", "is_valid_parenthesization", "kheapsort",
    "knapsack", "kth", "lcs_length", "levenshtein", "lis",
    "longest_common_subsequence", "max_sublist_sum", "mergesort",
    "minimum_spanning_tree", "next_palindrome", "next_permutation", "pascal",
    "possible_change", "powerset", "quicksort", "reverse_linked_list",
    "rpn_eval", "shortest_path_length", "shortest_path_lengths",
    "shortest_paths", "shunting_yard", "sieve", "sqrt", "subsequences",
    "to_base", "topological_ordering", "wrap",
})
QUIXBUGS_FLAT_PROGRAMS = sorted(QUIXBUGS_ALL_PROGRAMS - QUIXBUGS_GRAPH_BASED)


def _get(url: str, timeout: int = 30) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


def _split_visible_hidden(cases: list, visible_frac: float = 0.3) -> tuple[list, list]:
    """First `visible_frac` (min 1, capped so >=1 stays hidden) is visible."""
    n = len(cases)
    if n <= 1:
        return [], cases
    v = max(1, round(n * visible_frac))
    v = min(v, n - 1)
    return cases[:v], cases[v:]


def _render_quixbugs_test(entry_point: str, cases: list) -> str:
    """A flat, pytest-free assert script — same execution shape as the
    HumanEvalFix hidden test (see `_check` construction in `sandbox.py`'s
    caller): `python3 <file>.py` exits 0 on pass, raises on the first failure.
    """
    lines = [f"from solution import {entry_point}", ""]
    for i, (args, expected) in enumerate(cases):
        call_args = args if isinstance(args, list) else [args]
        lines.append(
            f"assert {entry_point}(*{call_args!r}) == {expected!r}, "
            f"'case {i}: {entry_point}(*{call_args!r}) != {expected!r}'"
        )
    if not cases:
        lines.append("pass  # no cases in this split")
    return "\n".join(lines) + "\n"


def fetch_humanevalfix(out_dir: Path) -> int:
    try:
        import pyarrow.parquet as pq  # noqa: PLC0415 — fetch-time only dep
    except ImportError:
        raise SystemExit(
            "pyarrow is required to read the HF parquet file — re-run as:\n"
            "  uv run --with pyarrow python3 scripts/eval/codebench/fetch_datasets.py "
            "--source humanevalfix"
        )

    meta = json.loads(_get(HF_API_URL).decode())
    revision = meta["sha"]
    raw = _get(HF_PARQUET_URL)
    tmp_parquet = out_dir / "_humanevalpack_python.parquet"
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_parquet.write_bytes(raw)
    rows = pq.read_table(tmp_parquet).to_pylist()
    tmp_parquet.unlink()

    tasks = []
    for r in rows:
        entry_point = r["entry_point"]
        buggy_code = r["declaration"] + r["buggy_solution"]
        visible_test_code = (
            f"from solution import {entry_point}\n\n" + r["example_test"]
            if r["example_test"].strip()
            else None
        )
        hidden_test_code = f"from solution import {entry_point}\n\n" + r["test"]
        tasks.append(Task_json(
            task_id=r["task_id"],
            dataset="humanevalfix",
            entry_point=entry_point,
            buggy_code=buggy_code,
            visible_test_code=visible_test_code,
            hidden_test_code=hidden_test_code,
            prompt=r["instruction"],
            bug_type=r["bug_type"],
        ))

    out_path = out_dir / "dataset.jsonl"
    with open(out_path, "w") as f:
        for t in tasks:
            f.write(json.dumps(t) + "\n")

    (out_dir / "PROVENANCE.md").write_text(
        "# HumanEvalFix — provenance\n\n"
        "- Source: `bigcode/humanevalpack` on Hugging Face, `python` config, `test` split\n"
        f"- Dataset commit/sha: `{revision}`\n"
        "- Licence: MIT (per HF dataset card `cardData.license`)\n"
        f"- Fetched: {json.dumps(meta.get('lastModified'))}\n"
        f"- Tasks: {len(tasks)}\n"
        "- Visible test = `example_test` field (1-2 assertions); hidden test = `test` "
        "field (the full held-out suite, 5-8+ assertions). Both fields ship in the "
        "dataset itself — this is not a synthesized split, unlike QuixBugs below.\n"
        "- `import`/`test_setup` fields are empty for every Python row (checked all "
        "164) — not used.\n"
        "- `bug_type` is carried through (6 categories: value/operator/variable/"
        "function misuse, missing/excess logic). See `datasets.HARDER_BUG_TYPES` for "
        "the harder/easier split this build uses to build a harder task pool.\n"
    )
    print(f"humanevalfix: {len(tasks)} tasks -> {out_path} (rev {revision[:12]})")
    return len(tasks)


def fetch_quixbugs(out_dir: Path) -> int:
    meta = json.loads(_get(QB_API_URL).decode())
    sha = meta["default_branch"]
    # Pin to a commit, not a branch, so a re-run is reproducible.
    commit = json.loads(_get(f"{QB_API_URL}/commits/{sha}").decode())
    revision = commit["sha"]
    raw_base = QB_RAW.format(sha=revision)

    out_dir.mkdir(parents=True, exist_ok=True)
    tasks = []
    skipped = []
    for name in QUIXBUGS_FLAT_PROGRAMS:
        try:
            buggy_code = _get(f"{raw_base}/python_programs/{name}.py").decode()
        except Exception as e:  # noqa: BLE001 — record and continue
            skipped.append((name, str(e)))
            continue
        try:
            cases_raw = _get(f"{raw_base}/json_testcases/{name}.json").decode()
        except Exception as e:  # noqa: BLE001
            skipped.append((name, str(e)))
            continue
        cases = [json.loads(line) for line in cases_raw.splitlines() if line.strip()]
        visible_cases, hidden_cases = _split_visible_hidden(cases)

        visible_test_code = (
            _render_quixbugs_test(name, visible_cases) if visible_cases else None
        )
        hidden_test_code = _render_quixbugs_test(name, hidden_cases)

        tasks.append(Task_json(
            task_id=f"QuixBugs/{name}",
            dataset="quixbugs",
            entry_point=name,
            buggy_code=buggy_code,
            visible_test_code=visible_test_code,
            hidden_test_code=hidden_test_code,
            prompt=(
                f"Write a Python function `{name}` that matches the docstring at the "
                f"bottom of `solution.py`. It currently fails some of its tests — find "
                f"and fix the bug."
            ),
        ))

    out_path = out_dir / "dataset.jsonl"
    with open(out_path, "w") as f:
        for t in tasks:
            f.write(json.dumps(t) + "\n")

    (out_dir / "PROVENANCE.md").write_text(
        "# QuixBugs (Python, flat subset) — provenance\n\n"
        "- Source: `jkoppel/QuixBugs` on GitHub\n"
        f"- Commit: `{revision}`\n"
        "- Licence: MIT (repo LICENSE, James Koppel, 2017-2019)\n"
        f"- Tasks: {len(tasks)} of the 40 total programs\n"
        "- **Scope decision**: the 9 graph-based programs "
        f"({', '.join(sorted(QUIXBUGS_GRAPH_BASED))}) are EXCLUDED from v1. Their tests "
        "are hand-written functions over a shared `Node` helper, not parametrized "
        "[input, expected] pairs, so the visible/hidden split this script does by "
        "slicing a case list doesn't apply cleanly. Follow-up, not silently dropped.\n"
        "- Visible/hidden split: first `round(0.3 * n)` cases (min 1, at least 1 held "
        "out) from `json_testcases/<name>.json` are visible; the rest are hidden. This "
        "split is OURS (QuixBugs ships one undifferentiated case list per program) — "
        "unlike HumanEvalFix, where visible/hidden are both authored upstream.\n"
        + (f"- Skipped (fetch error): {skipped}\n" if skipped else "")
    )
    print(f"quixbugs: {len(tasks)} tasks -> {out_path} (rev {revision[:12]})" + (
        f", {len(skipped)} skipped" if skipped else ""
    ))
    return len(tasks)


def Task_json(**kwargs) -> dict:
    """Build the JSONL row shape without importing `datasets.Task` (this
    script has no other reason to depend on it, and keeping it dependency-free
    of the rest of the package makes it runnable via a bare `uv run --with
    pyarrow python3 <path>` with no PYTHONPATH games)."""
    return kwargs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["humanevalfix", "quixbugs", "all"], default="all")
    ap.add_argument("--out", type=Path, default=DATA_ROOT)
    args = ap.parse_args()

    total = 0
    if args.source in ("humanevalfix", "all"):
        total += fetch_humanevalfix(args.out / "humanevalfix")
    if args.source in ("quixbugs", "all"):
        total += fetch_quixbugs(args.out / "quixbugs")
    print(f"total: {total} tasks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
