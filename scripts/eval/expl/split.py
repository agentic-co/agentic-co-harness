"""Deterministic stratified 80/50/65 split (EXP-L.md v3, "Split (changed)").

Strata = (user_goal, action_should_succeed). Seed 20260926, fixed. The method
is a running-remainder apportionment: strata are visited in a fixed
(sorted-key) order, each stratum's items are shuffled with a seeded RNG, and
each stratum's own edit/select/test counts are chosen to track the REMAINING
global targets (not the fixed global ratio), so per-stratum rounding error
cannot accumulate — the invariant `remaining_total == sum(remaining_targets)`
holds after every stratum and forces the last stratum's leftover items to
exactly zero out the remaining targets. That is what guarantees the global
totals land on exactly 80/50/65 (any fixed-ratio-per-stratum scheme does not,
once you have single-task strata, which this domain has three of).

This module only needs the tasks file's shape (dict of goal -> list of task
dicts with `action_should_succeed`); it does not need SOPBench importable, so
it is plain stdlib and unit-testable without the SOPBench venv.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

SEED = 20260926
SPLIT_NAMES = ("edit", "select", "test")
DEFAULT_TARGETS = {"edit": 80, "select": 50, "test": 65}


@dataclass(frozen=True)
class TaskRef:
    position: int
    user_goal: str
    action_should_succeed: bool

    @property
    def stratum(self) -> tuple[str, bool]:
        return (self.user_goal, self.action_should_succeed)


def load_task_refs(tasks_path: Path) -> list[TaskRef]:
    """Flatten a SOPBench `<domain>_tasks.json` in the SAME order
    `run_simulation`/`--task-ids` use: goal-dict order, then list order within
    each goal. Position `i` here is exactly the position `--task-ids` and the
    scorer's `idx` mean.
    """
    data = json.loads(Path(tasks_path).read_text())
    refs: list[TaskRef] = []
    pos = 0
    for goal, tasks in data.items():
        for task in tasks:
            refs.append(TaskRef(pos, goal, bool(task["action_should_succeed"])))
            pos += 1
    return refs


def stratified_split(
    refs: list[TaskRef],
    seed: int = SEED,
    targets: Optional[dict[str, int]] = None,
) -> dict[str, list[int]]:
    """Deterministic stratified split. Returns {"edit": [...], "select": [...],
    "test": [...]} of task positions, sorted ascending within each split.

    Deterministic in two independent senses that the tests check separately:
    same `refs`+`seed`+`targets` always produces the same split (reproduce),
    and the three lists partition `refs` exactly once each (partition).
    """
    targets = dict(targets or DEFAULT_TARGETS)
    total = len(refs)
    if sum(targets.values()) != total:
        raise ValueError(
            f"targets {targets} sum to {sum(targets.values())}, but {total} tasks were given"
        )

    by_stratum: dict[tuple[str, bool], list[int]] = {}
    for ref in refs:
        by_stratum.setdefault(ref.stratum, []).append(ref.position)

    rng = random.Random(seed)
    remaining_targets = dict(targets)
    remaining_total = total
    out: dict[str, list[int]] = {name: [] for name in SPLIT_NAMES}

    # Fixed key order so the result does not depend on dict/JSON insertion
    # order of the tasks file, only on the seed.
    for stratum in sorted(by_stratum, key=lambda s: (s[0], s[1])):
        items = list(by_stratum[stratum])
        rng.shuffle(items)
        n_s = len(items)
        if remaining_total <= 0:
            raise AssertionError("targets exhausted before tasks were — apportionment bug")

        # Proportional allocation against what's LEFT to place, not the fixed
        # global ratio — see module docstring for why.
        raw = {name: remaining_targets[name] * n_s / remaining_total for name in SPLIT_NAMES}
        floor = {name: int(raw[name]) for name in SPLIT_NAMES}
        leftover = n_s - sum(floor.values())
        # Largest-remainder method for the leftover units; ties broken by the
        # fixed split order (edit, select, test) for reproducibility.
        remainders = sorted(
            SPLIT_NAMES, key=lambda name: (-(raw[name] - floor[name]), SPLIT_NAMES.index(name))
        )
        counts = dict(floor)
        for name in remainders[:leftover]:
            counts[name] += 1

        cursor = 0
        for name in SPLIT_NAMES:
            take = counts[name]
            out[name].extend(items[cursor : cursor + take])
            cursor += take
            remaining_targets[name] -= take
        remaining_total -= n_s

    if any(remaining_targets[name] != 0 for name in SPLIT_NAMES):
        raise AssertionError(f"apportionment left a remainder: {remaining_targets}")

    for name in SPLIT_NAMES:
        out[name].sort()
    return out


def build_split_file(
    tasks_path: Path,
    out_path: Path,
    seed: int = SEED,
    targets: Optional[dict[str, int]] = None,
    domain: str = "hotel",
) -> dict:
    refs = load_task_refs(tasks_path)
    split = stratified_split(refs, seed=seed, targets=targets)
    strata = {
        f"{ref.user_goal}|{ref.action_should_succeed}": None for ref in refs
    }  # just documents the stratum keys used; not consumed downstream
    payload = {
        "domain": domain,
        "seed": seed,
        "targets": dict(targets or DEFAULT_TARGETS),
        "n_tasks": len(refs),
        "strata": sorted(strata.keys()),
        **split,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n")
    return payload


def load_split_file(path: Path) -> dict[str, list[int]]:
    payload = json.loads(Path(path).read_text())
    return {name: list(payload[name]) for name in SPLIT_NAMES}


def write_task_ids_file(positions: list[int], out_path: Path) -> Path:
    """`--task-ids` wants a bare JSON list of positions on disk."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(sorted(positions)))
    return out_path


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tasks", type=Path, required=True, help="SOPBench <domain>_tasks.json")
    ap.add_argument("--domain", default="hotel")
    ap.add_argument("--out", type=Path, required=True, help="where to write split.json")
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()

    payload = build_split_file(args.tasks, args.out, seed=args.seed, domain=args.domain)
    for name in SPLIT_NAMES:
        print(f"{name}: {len(payload[name])} tasks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
