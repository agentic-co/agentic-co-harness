#!/usr/bin/env python3
"""CLI entrypoint: run N tasks x arms x one model, write JSONL, print a table.

Smoke run (10 HumanEvalFix tasks, all 4 arms, z.ai GLM-4.7 — the default
endpoint, so no flags needed beyond --limit):

    uv run python3 scripts/eval/codebench/run_codebench.py \\
        --dataset humanevalfix --limit 10 --out evals/codebench/results/smoke

Full sweep against any OpenAI-compatible base URL + model (e.g. LM Studio),
using a 60-task stratified sample so 4 models x 4 arms stays tractable
(same 60 task_ids for every model, given the same --sample/--seed):

    uv run python3 scripts/eval/codebench/run_codebench.py \\
        --dataset all --sample 60 --seed 20260926 \\
        --base-url http://localhost:4242/v1 --model <model-id> \\
        --api-key-env NONE --concurrency 1 \\
        --out evals/codebench/results/<model-id>

`--api-key-env NONE` tells the endpoint no auth header is needed (local
servers). Omit --base-url/--model entirely to use the z.ai coding endpoint
(GLM-4.7, ZAI_API_KEY) this build smoke-tested against.

LM Studio with a single model loaded and `--parallel 1` serializes every
request server-side regardless of how many this runner has in flight —
`--concurrency 1` is the correct setting there: it is functionally safe at
higher concurrency (requests just queue), but buys no throughput and burns
into `--request-timeout-s`'s budget for no reason, since a request now waits
in the server's queue instead of just not being sent yet.

A run that got interrupted (Ctrl-C, timeout, crash) resumes in place:

    uv run python3 scripts/eval/codebench/run_codebench.py \\
        --dataset all --sample 60 --seed 20260926 --resume \\
        --base-url http://localhost:4242/v1 --model <model-id> \\
        --api-key-env NONE --concurrency 1 \\
        --out evals/codebench/results/<model-id>

`--resume` skips any (task_id, arm) pair already present in
`<out>/episodes.jsonl` and appends the rest — it does not re-derive the task
list from what's missing, so the SAME --sample/--seed/--dataset/--arms must
be passed again or the "already done" set is computed against the wrong job
list.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_PARENT = Path(__file__).resolve().parents[1]
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

from codebench.agent import run_episode  # noqa: E402
from codebench.arms import ARMS  # noqa: E402
from codebench.datasets import (  # noqa: E402
    Task, filter_harder, load_humanevalfix, load_quixbugs, stratified_sample,
)
from codebench.metrics import format_table, load_episodes, summarize_arm  # noqa: E402
from codebench.provider import DEFAULT_TIMEOUT_S, endpoint_from_args  # noqa: E402

import json  # noqa: E402


def _load_tasks(
    dataset: str, limit: int | None, sample: int | None, seed: int, difficulty: str,
) -> list[Task]:
    tasks: list[Task] = []
    if dataset in ("humanevalfix", "all"):
        tasks += load_humanevalfix()
    if dataset in ("quixbugs", "all"):
        tasks += load_quixbugs()

    if difficulty == "harder":
        tasks = filter_harder(tasks)

    if sample is not None:
        # Stratified over the WHOLE loaded set (proportional across
        # datasets), same task_ids every time for a given (sample, seed) —
        # the property a cross-model sweep needs.
        return stratified_sample(tasks, sample, seed)

    if limit is not None:
        # Deterministic: first `limit` of EACH dataset requested, in file
        # order — not a random sample, so a repeat run with the same --limit
        # touches the same tasks.
        by_ds: dict[str, list[Task]] = {}
        for t in tasks:
            by_ds.setdefault(t.dataset, []).append(t)
        tasks = [t for ds_tasks in by_ds.values() for t in ds_tasks[:limit]]
    return tasks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", choices=["humanevalfix", "quixbugs", "all"], default="all")
    ap.add_argument("--limit", type=int, default=None, help="first N tasks per dataset")
    ap.add_argument("--sample", type=int, default=None,
                     help="deterministic stratified sample of N tasks across the loaded "
                          "dataset(s) — same task_ids every run for a given (--sample, --seed). "
                          "Takes priority over --limit if both are given.")
    ap.add_argument("--seed", type=int, default=20260926, help="only used with --sample")
    ap.add_argument("--difficulty", choices=["all", "harder"], default="all",
                     help="'harder' keeps all QuixBugs plus only the HumanEvalFix bug_types "
                          "in codebench.datasets.HARDER_BUG_TYPES (value/operator/variable/"
                          "function misuse) — 131 of 195 tasks. Applied BEFORE --sample/--limit.")
    ap.add_argument("--mode", choices=["default", "ticket-only"], default="default",
                     help="'ticket-only' hides the visible test entirely — the agent gets "
                          "only the buggy code + the natural-language task description, "
                          "matching a real bug ticket. asop's REPRODUCE gate then has to "
                          "infer intended behavior with no test to lean on.")
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--api-key-env", default=None, help="'NONE' for no-auth local servers")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--max-turns", type=int, default=8)
    ap.add_argument("--max-tool-calls", type=int, default=20)
    ap.add_argument("--max-refusals", type=int, default=3)
    ap.add_argument("--tool-timeout-s", type=int, default=10)
    ap.add_argument("--episode-timeout-s", type=int, default=240)
    ap.add_argument("--request-timeout-s", type=int, default=DEFAULT_TIMEOUT_S,
                     help="per-chat-completion HTTP timeout — raise this for a serialized "
                          "local backend (e.g. LM Studio --parallel 1) under concurrency > 1, "
                          "where a request can sit queued behind others before it's served")
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--resume", action="store_true",
                     help="skip (task_id, arm) pairs already in <out>/episodes.jsonl and "
                          "append the rest — pass the SAME --dataset/--sample/--seed/--arms "
                          "as the interrupted run, they are not recovered from the output file")
    args = ap.parse_args()

    api_key_env = None if args.api_key_env == "NONE" else args.api_key_env
    endpoint = endpoint_from_args(args.base_url, args.model, api_key_env)
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    for a in arms:
        if a not in ARMS:
            raise SystemExit(f"unknown arm {a!r}, choose from {ARMS}")

    tasks = _load_tasks(args.dataset, args.limit, args.sample, args.seed, args.difficulty)
    if not tasks:
        raise SystemExit("no tasks loaded — check --dataset and that fetch_datasets.py has run")

    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / "episodes.jsonl"
    lock = threading.Lock()

    jobs = [(t, a) for t in tasks for a in arms]
    already_done: set[tuple[str, str]] = set()
    if args.resume and out_path.exists():
        already_done = {(e["task_id"], e["arm"]) for e in load_episodes(out_path)}
        jobs = [(t, a) for (t, a) in jobs if (t.task_id, a) not in already_done]
        print(f"--resume: {len(already_done)} episodes already in {out_path}, "
              f"{len(jobs)} remaining")
    elif args.resume:
        print(f"--resume: no existing {out_path}, running all {len(jobs)} episodes")

    print(
        f"running {len(jobs)} episodes ({len(tasks)} tasks x {len(arms)} arms, "
        f"difficulty={args.difficulty}, mode={args.mode}"
        f"{f', {len(already_done)} already done' if already_done else ''}) "
        f"against {endpoint.model} @ {endpoint.base_url} (concurrency={args.concurrency})"
    )

    done_count = len(already_done)
    total_jobs = len(tasks) * len(arms)
    started = time.monotonic()

    def _run_one(job):
        task, arm = job
        return run_episode(
            task, arm, endpoint,
            max_turns=args.max_turns, max_tool_calls=args.max_tool_calls,
            max_refusals=args.max_refusals, tool_timeout_s=args.tool_timeout_s,
            episode_timeout_s=args.episode_timeout_s, temperature=args.temperature,
            request_timeout_s=args.request_timeout_s,
            ticket_only=(args.mode == "ticket-only"),
        )

    file_mode = "a" if args.resume else "w"
    with open(out_path, file_mode) as f, ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(_run_one, job): job for job in jobs}
        for fut in as_completed(futures):
            task, arm = futures[fut]
            try:
                ep = fut.result()
            except Exception as e:  # noqa: BLE001 — record and keep going
                print(f"  ERROR {task.task_id} [{arm}]: {e}", file=sys.stderr)
                continue
            with lock:
                f.write(json.dumps(ep.to_json()) + "\n")
                f.flush()
                done_count += 1
            mark = "PASS" if ep.hidden_pass else "FAIL"
            print(f"  [{done_count}/{total_jobs}] {task.task_id} [{arm}] {ep.status} {mark} "
                  f"({ep.turns}t/{ep.tool_calls}tc/{ep.wall_time_s:.1f}s)")

    elapsed = time.monotonic() - started
    print(f"\ndone in {elapsed:.1f}s -> {out_path}")

    episodes = load_episodes(out_path)
    by_arm: dict[str, list[dict]] = {}
    for e in episodes:
        by_arm.setdefault(e["arm"], []).append(e)
    summaries = [summarize_arm(by_arm[a]) for a in arms if a in by_arm]
    print()
    print(format_table(summaries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
