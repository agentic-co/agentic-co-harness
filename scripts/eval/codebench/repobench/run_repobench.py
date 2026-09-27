#!/usr/bin/env python3
"""CLI entrypoint for the repo-bug tier: N mutant tasks x arms x one model.

Smoke (2 bugs x 4 arms — the size to run FIRST, against z.ai or a free local
endpoint, before committing to the full 60):

    uv run python3 scripts/eval/codebench/repobench/run_repobench.py \\
        --limit 2 --out evals/codebench/results/repobench-smoke

Full 60-task run against LM Studio (one model loaded, `--parallel 1`, hence
`--concurrency 1` — same reasoning as the single-file tier):

    uv run python3 scripts/eval/codebench/repobench/run_repobench.py \\
        --base-url http://localhost:4242/v1 --model <model-id> \\
        --api-key-env NONE --concurrency 1 --request-timeout-s 600 --resume \\
        --out evals/codebench/results/repobench-local-<key>

Every full-suite run (both the gate's VALIDATE step and final grading) is a
real `pytest` subprocess against a throwaway temp copy of the repo snapshot
— nothing here mutates `evals/codebench/data/repobench/<repo>/repo/` itself.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parents[2]
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from codebench.metrics import format_table, load_episodes, summarize_arm  # noqa: E402
from codebench.provider import DEFAULT_MAX_TOKENS, DEFAULT_TIMEOUT_S, endpoint_from_args  # noqa: E402
from codebench.repobench.mutate import find_sites, apply_mutation  # noqa: E402
from codebench.repobench.repo_agent import run_repo_episode  # noqa: E402
from codebench.repobench.repo_arms import ARMS  # noqa: E402
from codebench.repobench.repos import by_name  # noqa: E402

DATA_ROOT = Path(__file__).resolve().parents[4] / "evals" / "codebench" / "data" / "repobench"


def _load_tasks(mutants_path: Path, repo_filter: str, limit: int | None) -> list[dict]:
    tasks = []
    with open(mutants_path) as f:
        for line in f:
            line = line.strip()
            if line:
                tasks.append(json.loads(line))
    if repo_filter != "all":
        tasks = [t for t in tasks if t["repo"] == repo_filter]
    if limit is not None:
        by_repo: dict[str, list[dict]] = {}
        for t in tasks:
            by_repo.setdefault(t["repo"], []).append(t)
        tasks = [t for group in by_repo.values() for t in group[:limit]]
    return tasks


def _mutated_source_for(task: dict) -> str:
    """Recompute the mutant's exact source text from the pristine snapshot +
    the recorded (lineno, kind, detail) — cheaper and more honest than
    storing 60 full mutated files: this IS the same deterministic function
    `generate_tasks.py` used, run again on the still-pristine snapshot."""
    spec = by_name(task["repo"])
    snapshot = DATA_ROOT / spec.name / "repo"
    original = (snapshot / task["mutated_file"]).read_text()
    sites = find_sites(original)
    site = next(
        s for s in sites
        if s.lineno == task["mutation_lineno"] and s.kind == task["mutation_kind"]
        and s.detail == task["mutation_detail"]
    )
    return apply_mutation(original, site)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mutants", type=Path, default=DATA_ROOT / "mutants.jsonl")
    ap.add_argument("--repo", choices=["all", "more-itertools", "toolz", "boltons"], default="all")
    ap.add_argument("--limit", type=int, default=None, help="first N tasks per repo")
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--api-key-env", default=None, help="'NONE' for no-auth local servers")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--max-turns", type=int, default=20)
    ap.add_argument("--max-tool-calls", type=int, default=40)
    ap.add_argument("--max-refusals", type=int, default=4)
    ap.add_argument("--tool-timeout-s", type=int, default=20)
    ap.add_argument("--episode-timeout-s", type=int, default=600)
    ap.add_argument("--request-timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--max-tokens", type=int, default=None,
                     help="per-call completion token cap, passed straight through to the "
                          "provider (default: codebench.provider.DEFAULT_MAX_TOKENS). Logged "
                          "on every episode; a 'length' finish_reason on any turn sets "
                          "any_truncated=True in that episode's record.")
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    api_key_env = None if args.api_key_env == "NONE" else args.api_key_env
    endpoint = endpoint_from_args(args.base_url, args.model, api_key_env)
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    for a in arms:
        if a not in ARMS:
            raise SystemExit(f"unknown arm {a!r}, choose from {ARMS}")

    tasks = _load_tasks(args.mutants, args.repo, args.limit)
    if not tasks:
        raise SystemExit(f"no tasks loaded from {args.mutants} — run generate_tasks.py first")

    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / "episodes.jsonl"
    lock = threading.Lock()

    jobs = [(t, a) for t in tasks for a in arms]
    already_done: set[tuple[str, str]] = set()
    if args.resume and out_path.exists():
        already_done = {(e["task_id"], e["arm"]) for e in load_episodes(out_path)}
        jobs = [(t, a) for (t, a) in jobs if (t["task_id"], a) not in already_done]
        print(f"--resume: {len(already_done)} episodes already in {out_path}, {len(jobs)} remaining")

    print(
        f"running {len(jobs)} episodes ({len(tasks)} tasks x {len(arms)} arms"
        f"{f', {len(already_done)} already done' if already_done else ''}) "
        f"against {endpoint.model} @ {endpoint.base_url} (concurrency={args.concurrency})"
    )

    done_count = len(already_done)
    total_jobs = len(tasks) * len(arms)
    started = time.monotonic()

    def _run_one(job):
        task, arm = job
        spec = by_name(task["repo"])
        snapshot = DATA_ROOT / spec.name / "repo"
        mutated_source = _mutated_source_for(task)
        return run_repo_episode(
            task, arm, spec, snapshot, mutated_source, endpoint,
            max_turns=args.max_turns, max_tool_calls=args.max_tool_calls,
            max_refusals=args.max_refusals, tool_timeout_s=args.tool_timeout_s,
            episode_timeout_s=args.episode_timeout_s, temperature=args.temperature,
            request_timeout_s=args.request_timeout_s,
            max_tokens=args.max_tokens if args.max_tokens is not None else DEFAULT_MAX_TOKENS,
        )

    file_mode = "a" if args.resume else "w"
    with open(out_path, file_mode) as f, ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(_run_one, job): job for job in jobs}
        for fut in as_completed(futures):
            task, arm = futures[fut]
            try:
                ep = fut.result()
            except Exception as e:  # noqa: BLE001 — record and keep going
                print(f"  ERROR {task['task_id']} [{arm}]: {e}", file=sys.stderr)
                continue
            with lock:
                f.write(json.dumps(ep.to_json()) + "\n")
                f.flush()
                done_count += 1
            mark = "PASS" if ep.hidden_pass else "FAIL"
            trunc = " TRUNCATED" if ep.any_truncated else ""
            print(f"  [{done_count}/{total_jobs}] {task['task_id']} [{arm}] {ep.status} {mark} "
                  f"(loc={ep.localisation} regr={ep.regression_count} "
                  f"{ep.turns}t/{ep.tool_calls}tc/{ep.wall_time_s:.1f}s){trunc}")

    elapsed = time.monotonic() - started
    print(f"\ndone in {elapsed:.1f}s -> {out_path}")

    episodes = load_episodes(out_path)
    by_arm: dict[str, list[dict]] = {}
    for e in episodes:
        by_arm.setdefault(e["arm"], []).append(e)
    summaries = [summarize_arm(by_arm[a]) for a in arms if a in by_arm]
    print()
    print(format_table(summaries))

    truncated = [e for e in episodes if e.get("any_truncated")]
    print(f"\nmax_tokens used: {episodes[0].get('max_tokens') if episodes else '?'} "
          f"| truncated episodes (finish_reason='length' on some turn): {len(truncated)}/{len(episodes)}")
    if truncated:
        print("  " + ", ".join(f"{e['task_id']}[{e['arm']}]" for e in truncated))

    damaged = [e for e in episodes if e.get("damaged_files")]
    print(f"damaged-file episodes (a source file shrank >20% vs its buggy start): "
          f"{len(damaged)}/{len(episodes)}")
    if damaged:
        by_arm_damage: dict[str, int] = {}
        for e in damaged:
            by_arm_damage[e["arm"]] = by_arm_damage.get(e["arm"], 0) + 1
        print("  per arm: " + ", ".join(f"{a}={n}" for a, n in sorted(by_arm_damage.items())))
        for e in damaged:
            print(f"  {e['task_id']}[{e['arm']}]: {e['damaged_files']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
