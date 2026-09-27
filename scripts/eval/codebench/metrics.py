"""Aggregation over a run's episodes: pass@1, false-done, cost/turns, and
exact (paired) McNemar tests between arms on the same task set.

No scipy — `math.comb` is stdlib since Python 3.8 and exact McNemar is a
closed-form sum over it, which is all this needs (small n, this is not a
place to trade a dependency for a table lookup).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path


def load_episodes(path: Path) -> list[dict]:
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


@dataclass
class ArmSummary:
    arm: str
    n: int
    pass_at_1: float
    false_done_rate: float
    finished_rate: float  # fraction that reached status == "finished"
    repro_written_rate: float  # asop/asop-nogate only; NaN-ish (None) for bare/pva
    repro_validity_rate: float | None  # among those that wrote one
    mean_turns: float
    mean_tool_calls: float
    mean_prompt_tokens: float
    mean_completion_tokens: float
    mean_wall_time_s: float


def summarize_arm(episodes: list[dict]) -> ArmSummary:
    n = len(episodes)
    if n == 0:
        raise ValueError("no episodes to summarize")
    arm = episodes[0]["arm"]

    def _mean(key: str) -> float:
        return sum(e[key] for e in episodes) / n

    passed = sum(1 for e in episodes if e.get("hidden_pass"))
    false_done = sum(1 for e in episodes if e.get("false_done"))
    finished = sum(1 for e in episodes if e.get("status") == "finished")
    wrote_repro = [e for e in episodes if e.get("repro_written")]
    valid_repro = [e for e in wrote_repro if e.get("repro_validity")]

    return ArmSummary(
        arm=arm,
        n=n,
        pass_at_1=passed / n,
        false_done_rate=false_done / n,
        finished_rate=finished / n,
        repro_written_rate=len(wrote_repro) / n,
        repro_validity_rate=(len(valid_repro) / len(wrote_repro)) if wrote_repro else None,
        mean_turns=_mean("turns"),
        mean_tool_calls=_mean("tool_calls"),
        mean_prompt_tokens=_mean("prompt_tokens"),
        mean_completion_tokens=_mean("completion_tokens"),
        mean_wall_time_s=_mean("wall_time_s"),
    )


def exact_mcnemar(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value for discordant pair counts b, c
    (b = arm1-pass/arm2-fail, c = arm1-fail/arm2-pass)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1))
    p = 2 * tail * (0.5 ** n)
    return min(p, 1.0)


def paired_compare(episodes_a: list[dict], episodes_b: list[dict]) -> dict:
    """McNemar over hidden_pass, paired by task_id. Both episode lists must
    cover the same task set (a subset intersection is used if not, and the
    dropped ids are reported so a mismatch isn't silently averaged away)."""
    by_id_a = {e["task_id"]: e for e in episodes_a}
    by_id_b = {e["task_id"]: e for e in episodes_b}
    common = sorted(set(by_id_a) & set(by_id_b))
    dropped = sorted((set(by_id_a) | set(by_id_b)) - set(common))

    b = c = both_pass = both_fail = 0
    for tid in common:
        pa, pb = bool(by_id_a[tid].get("hidden_pass")), bool(by_id_b[tid].get("hidden_pass"))
        if pa and not pb:
            b += 1
        elif not pa and pb:
            c += 1
        elif pa and pb:
            both_pass += 1
        else:
            both_fail += 1

    return {
        "n_pairs": len(common),
        "dropped_task_ids": dropped,
        "both_pass": both_pass,
        "both_fail": both_fail,
        "a_only": b,
        "b_only": c,
        "p_value_exact_mcnemar": exact_mcnemar(b, c),
    }


def format_table(summaries: list[ArmSummary]) -> str:
    header = (
        f"{'arm':<14}{'n':>4}{'pass@1':>9}{'false_done':>12}{'finished':>10}"
        f"{'turns':>8}{'tool_calls':>11}{'prompt_tok':>12}{'compl_tok':>11}{'wall_s':>9}"
    )
    lines = [header, "-" * len(header)]
    for s in summaries:
        lines.append(
            f"{s.arm:<14}{s.n:>4}{s.pass_at_1:>9.3f}{s.false_done_rate:>12.3f}{s.finished_rate:>10.3f}"
            f"{s.mean_turns:>8.1f}{s.mean_tool_calls:>11.1f}{s.mean_prompt_tokens:>12.0f}"
            f"{s.mean_completion_tokens:>11.0f}{s.mean_wall_time_s:>9.1f}"
        )
    return "\n".join(lines)
