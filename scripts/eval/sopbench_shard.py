"""Run one SOPBench arm as N shards against N model instances, then merge.

Why this exists: an arm is ~112-134 sequential conversations against one local
model, 1-2 h on `gpt-oss-20b`. Each shard gets its own model copy, so requests
are never batched inside one model.

⚠️ MEASURED 2026-09-24, M3 Ultra, 2 copies of `gpt-oss-20b`: per-task time rose
from 12.8 s alone to ~20-22 s concurrent — about **1.2x** total throughput, not
the ~1.7x predicted from memory bandwidth. These prompts are long (document +
host rules), so prompt processing is compute-bound and the copies contend for
the GPU. 3 and 4 copies: 66 s and 65 s wall for the same 6 tasks (77 s on one
copy, 68 s on two), every run 6/6 identical to the unsharded reference.
Throughput saturates at ~1.2-1.3x; two copies is the useful setting.

🛑 MEASURED, same 6 tasks: ONE copy at `--parallel 4`, fed by 2 and 4 shards,
is SLOWER (77 s, 84 s) and NOT identical — 3/6 and 2/6 trajectories match
the reference. Mostly wording, but at 2 shards task 1 changed behaviour:
the reference opened the account and applied for the card, the batched run
exited after the username check. That is an outcome flip from batching
alone, which is exactly why this module uses one copy per shard.

⚠️ That distinction is the point. `lms load --parallel N` batches concurrent
requests through one instance, and batched kernels change floating-point
reduction order — at temperature 0 that can flip a borderline trajectory.
Every published arm ran one request at a time. N instances at `--parallel 1`
each keeps the per-request numerics of the published arms, so a shard run is
the same experiment, only faster. Loading instances is the operator's job:

    lms load openai/gpt-oss-20b --identifier gpt-oss-20b-s0 --parallel 1
    lms load openai/gpt-oss-20b --identifier gpt-oss-20b-s1 --parallel 1

How sharding rides on SOPBench without editing it: `run_simulation.main`
resumes POSITIONALLY — task `i` is skipped iff `results[i]` already holds
enough interactions. So a shard seeds its results list with a placeholder
interaction at every position it does NOT own and an empty slot at every
position it does; the benchmark's own loop then runs exactly the owned tasks,
and a re-run of the same shard resumes losslessly as it always has.

Shards are INTERLEAVED (task i -> shard i mod N), not contiguous: SOPBench
orders tasks by action type, so contiguous blocks would give one shard all of
one goal's long conversations and the slowest shard would set the wall-clock.

`merge` reassembles one results file under the canonical filename the scorer
already reads, and refuses anything that is not exactly one real result per
position.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PLACEHOLDER_KEY = "_shard_placeholder"


def parse_shard(spec: str) -> tuple[int, int]:
    """`"1/3"` -> `(1, 3)`. Zero-based index, so shards of 3 are 0/3, 1/3, 2/3."""
    try:
        k_s, n_s = spec.split("/")
        k, n = int(k_s), int(n_s)
    except ValueError:
        raise ValueError(f"--shard must look like K/N, got {spec!r}") from None
    if n < 1 or not 0 <= k < n:
        raise ValueError(f"--shard {spec!r}: need 0 <= K < N")
    return k, n


def owns(i: int, k: int, n: int, subset: frozenset | None = None) -> bool:
    """Shard k of n runs position i iff i is its residue AND (if a task subset
    is given) i is selected. `--task-ids` alone is shard 0/1 with a subset."""
    return i % n == k and (subset is None or i in subset)


def load_subset(path: str | Path | None) -> frozenset | None:
    """A task subset: a JSON list of positional task indices, or None for all."""
    if not path:
        return None
    ids = json.loads(Path(path).read_text())
    if not isinstance(ids, list) or not all(isinstance(i, int) and i >= 0 for i in ids):
        raise SystemExit(f"{path}: expected a JSON list of non-negative task positions")
    return frozenset(ids)


def is_placeholder(entry: Any) -> bool:
    runs = entry.get("interactions") if isinstance(entry, dict) else None
    return bool(runs) and isinstance(runs[0], dict) and bool(runs[0].get(PLACEHOLDER_KEY))


def is_done(entry: Any) -> bool:
    """A real, completed result: has interactions and is not a placeholder."""
    return isinstance(entry, dict) and bool(entry.get("interactions")) and not is_placeholder(entry)


def seed_results(existing: list, n_tasks: int, k: int, n: int,
                 subset: frozenset | None = None) -> list:
    """The list `run_simulation` should believe it loaded, for shard k of n.

    Owned positions keep whatever the file already had (a finished result, so a
    resume skips it) or get an empty slot (so it runs). Every other position
    gets a placeholder interaction, which satisfies the benchmark's
    `len(runs) < num_run_per_interaction` check and is skipped.

    Refuses a file holding a REAL result at a position this shard does not own:
    that is a file from a different shard count (or an unsharded run) sharing
    the directory, and silently overwriting it would discard work.
    """
    out = list(existing[:n_tasks])
    for i, entry in enumerate(out):
        if not owns(i, k, n, subset) and is_done(entry):
            raise SystemExit(
                f"shard {k}/{n}: position {i} already holds a real result it does not own — "
                "this output dir was written by a different shard layout or an unsharded run"
            )
    while len(out) < n_tasks:
        out.append({"interactions": []})
    for i in range(n_tasks):
        if not owns(i, k, n, subset):
            out[i] = {"interactions": [{PLACEHOLDER_KEY: True}]}
        elif not isinstance(out[i], dict) or "interactions" not in out[i]:
            out[i] = {"interactions": []}
    return out


def merge_results(shards: list[list], canonical_model: str | None = None,
                  subset: frozenset | None = None) -> list:
    """One results list from N shard lists. Exactly one real result per position.

    With `subset`, "exactly one" is required only at selected positions; every
    other position must hold NO real result and is written as an empty slot
    (`interactions: []`), which the scorer already skips — so a subset arm's
    file keeps SOPBench's positional indexing and scores on the subset alone.

    `canonical_model` rewrites `setup.assistant_agent.model` from the shard's
    instance identifier (e.g. `gpt-oss-20b-s1`) to the model name every other
    arm records, keeping the identifier under `.instance` so nothing is hidden.
    """
    if not shards:
        raise SystemExit("merge: no shard files given")
    width = max(len(s) for s in shards)
    merged: list = []
    problems: list[str] = []
    for i in range(width):
        real = [s[i] for s in shards if i < len(s) and is_done(s[i])]
        if subset is not None and i not in subset:
            if real:
                problems.append(f"position {i}: {len(real)} real results outside the subset")
            merged.append({"interactions": [], "_not_in_subset": True})
            continue
        if len(real) != 1:
            problems.append(f"position {i}: {len(real)} real results (need exactly 1)")
            merged.append(None)
            continue
        entry = json.loads(json.dumps(real[0]))  # deep copy, JSON-shaped
        agent = entry.get("setup", {}).get("assistant_agent")
        if canonical_model and isinstance(agent, dict) and agent.get("model") != canonical_model:
            agent["instance"] = agent.get("model")
            agent["model"] = canonical_model
        merged.append(entry)
    if problems:
        head = "\n  ".join(problems[:10])
        more = f"\n  ... and {len(problems) - 10} more" if len(problems) > 10 else ""
        raise SystemExit(f"merge refused — {len(problems)} bad positions:\n  {head}{more}")
    goals = {}
    for s in shards:
        for i, e in enumerate(s):
            if is_done(e):
                goals.setdefault(i, e.get("task", {}).get("user_goal"))
    for i, e in enumerate(merged):
        if subset is not None and i not in subset:
            continue
        if e.get("task", {}).get("user_goal") != goals.get(i):
            raise SystemExit(f"merge refused — position {i} task identity disagrees across shards")
    return merged


def merge_stats(stats: list[dict], aggregate) -> dict:
    """Concatenate shard stats rows and recompute the summary with the runner's
    own `aggregate_rows`, so a merged summary cannot drift from an unsharded one.
    """
    rows = [r for s in stats for r in s.get("rows", [])]
    base = dict(stats[0].get("summary", {}))
    base.update(aggregate(rows))
    base["ungated_tripwire_calls"] = sum(
        s.get("summary", {}).get("ungated_tripwire_calls", 0) for s in stats
    )
    base["shards"] = len(stats)
    return {"summary": base, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("merge", help="merge shard result files into one canonical file")
    m.add_argument("--out", type=Path, required=True, help="merged results file to write")
    m.add_argument("--canonical-model", default="openai/gpt-oss-20b")
    m.add_argument("--stats", type=Path, nargs="*", default=[], help="shard stats files")
    m.add_argument("--stats-out", type=Path, default=None)
    m.add_argument("--subset", type=Path, default=None,
                   help="the --task-ids file the shards ran; checked only over its positions")
    m.add_argument("shards", type=Path, nargs="+", help="shard results files")
    args = ap.parse_args()

    shards = [json.loads(p.read_text()) for p in args.shards]
    merged = merge_results(shards, args.canonical_model, load_subset(args.subset))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(merged, indent=4))
    print(f"merged {len(merged)} results from {len(shards)} shards -> {args.out}", file=sys.stderr)

    if args.stats:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from run_sopbench_asop import aggregate_rows

        out = merge_stats([json.loads(p.read_text()) for p in args.stats], aggregate_rows)
        if args.stats_out:
            args.stats_out.parent.mkdir(parents=True, exist_ok=True)
            args.stats_out.write_text(json.dumps(out, indent=2))
        print(json.dumps(out["summary"], indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
