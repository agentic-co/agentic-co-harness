"""Task model + loaders for the two codebench datasets.

Both datasets normalize to the same shape (`Task`) so the sandbox, agent loop,
and metrics never need to know which dataset a task came from. The raw,
dataset-specific fetch/normalize logic lives in `fetch_datasets.py`; this
module only reads the already-normalized JSONL this repo carries under
`evals/codebench/data/<dataset>/dataset.jsonl`.

Every task separates VISIBLE from HIDDEN tests at load time. Hidden tests are
never handed to the sandbox tools the agent can call — see `sandbox.py`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_ROOT = REPO_ROOT / "evals" / "codebench" / "data"


@dataclass(frozen=True)
class Task:
    task_id: str
    dataset: str  # "humanevalfix" | "quixbugs"
    entry_point: str
    buggy_code: str  # full initial content of solution.py
    visible_test_code: Optional[str]  # content of visible_test.py, or None
    hidden_test_code: str  # content of hidden_test.py — NEVER given to the sandbox
    prompt: str  # what the agent is told about the task (docstring/instruction)
    extra_files: dict[str, str] = field(default_factory=dict)  # e.g. node.py helper
    # HumanEvalFix's own difficulty taxonomy (bigcode/humanevalpack's `bug_type`
    # field: "value misuse", "missing logic", "excess logic", "operator misuse",
    # "variable misuse", "function misuse"). None for QuixBugs, which ships no
    # such field. See `HARDER_BUG_TYPES` for how this is used to build a harder
    # task pool.
    bug_type: Optional[str] = None

    def to_json(self) -> dict:
        d = {
            "task_id": self.task_id,
            "dataset": self.dataset,
            "entry_point": self.entry_point,
            "buggy_code": self.buggy_code,
            "visible_test_code": self.visible_test_code,
            "hidden_test_code": self.hidden_test_code,
            "prompt": self.prompt,
        }
        if self.extra_files:
            d["extra_files"] = self.extra_files
        if self.bug_type:
            d["bug_type"] = self.bug_type
        return d

    @staticmethod
    def from_json(d: dict) -> "Task":
        return Task(
            task_id=d["task_id"],
            dataset=d["dataset"],
            entry_point=d["entry_point"],
            buggy_code=d["buggy_code"],
            visible_test_code=d.get("visible_test_code"),
            hidden_test_code=d["hidden_test_code"],
            prompt=d["prompt"],
            extra_files=d.get("extra_files") or {},
            bug_type=d.get("bug_type"),
        )


def _load_jsonl(path: Path) -> list[dict]:
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def load_humanevalfix(data_root: Path = DATA_ROOT) -> list[Task]:
    path = data_root / "humanevalfix" / "dataset.jsonl"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} missing — run "
            "`uv run --with pyarrow python3 scripts/eval/codebench/fetch_datasets.py "
            "--source humanevalfix` first"
        )
    return [Task.from_json(d) for d in _load_jsonl(path)]


def load_quixbugs(data_root: Path = DATA_ROOT) -> list[Task]:
    path = data_root / "quixbugs" / "dataset.jsonl"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} missing — run "
            "`python3 scripts/eval/codebench/fetch_datasets.py --source quixbugs` first"
        )
    return [Task.from_json(d) for d in _load_jsonl(path)]


def load_all(data_root: Path = DATA_ROOT) -> list[Task]:
    tasks: list[Task] = []
    hef = data_root / "humanevalfix" / "dataset.jsonl"
    qb = data_root / "quixbugs" / "dataset.jsonl"
    if hef.exists():
        tasks += load_humanevalfix(data_root)
    if qb.exists():
        tasks += load_quixbugs(data_root)
    return tasks


def by_id(tasks: list[Task], task_id: str) -> Task:
    for t in tasks:
        if t.task_id == task_id:
            return t
    raise KeyError(task_id)


# Pre-registered BEFORE any local-model harder-pool run (2026-09-26) — a
# content-based split of HumanEvalFix's own `bug_type` taxonomy, not a split
# chosen by looking at which categories any model got wrong. The criterion:
# does the bug change the VISIBLE SHAPE of the code (a line present or
# absent — spottable by comparing structure to the docstring alone) or just
# one TOKEN inside an otherwise-plausible line (spottable only by reasoning
# about semantics/edge cases)?
#
#   HARDER  (single-token substitution, code still "looks right"):
#     value misuse, operator misuse, variable misuse, function misuse
#   EASIER  (a whole line's worth of logic missing or added):
#     missing logic, excess logic
#
# QuixBugs carries no such field — every one of its 31 flat-subset programs
# is treated as part of the harder pool by default, per the run brief.
HARDER_BUG_TYPES = frozenset({"value misuse", "operator misuse", "variable misuse", "function misuse"})
EASIER_BUG_TYPES = frozenset({"missing logic", "excess logic"})


def filter_harder(tasks: list[Task]) -> list[Task]:
    """QuixBugs tasks pass through unchanged; HumanEvalFix tasks are kept
    only if their `bug_type` is in `HARDER_BUG_TYPES`."""
    return [
        t for t in tasks
        if t.dataset != "humanevalfix" or t.bug_type in HARDER_BUG_TYPES
    ]


def stratified_sample(tasks: list[Task], n: int, seed: int) -> list[Task]:
    """A deterministic sample proportional to each dataset's share of
    `tasks`, so a 60-task sample pulls roughly HumanEvalFix's 164/195 and
    QuixBugs's 31/195 share rather than exhausting the smaller one.

    Deterministic in the sense that matters for a cross-model sweep: the same
    `(n, seed)` on the same on-disk datasets always selects the same
    `task_id`s, so every model in the sweep sees an identical task set —
    `random.Random(seed)` is seeded once per dataset group with an
    index derived from the group name, not consumed sequentially across
    groups, so adding/removing a dataset doesn't reshuffle the others' picks.
    """
    import random

    by_ds: dict[str, list[Task]] = {}
    for t in tasks:
        by_ds.setdefault(t.dataset, []).append(t)
    total = len(tasks)
    if n >= total:
        return list(tasks)

    picked: list[Task] = []
    remaining = n
    ds_names = sorted(by_ds)
    for i, ds in enumerate(ds_names):
        group = by_ds[ds]
        is_last = i == len(ds_names) - 1
        share = remaining if is_last else round(n * len(group) / total)
        share = min(share, len(group))
        rng = random.Random(f"{seed}:{ds}")
        idx = sorted(rng.sample(range(len(group)), share))
        picked += [group[j] for j in idx]
        remaining -= share
    return sorted(picked, key=lambda t: t.task_id)
