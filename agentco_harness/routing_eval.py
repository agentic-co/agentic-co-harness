"""Evidence-based model routing from the production cost ledger.

WHY THIS EXISTS
---------------
``Plans/ModelRoutingEval.md`` (July 2026) specified a synthetic harness: ~20
graded samples per (task type x model) cell, replayed offline. Since it was
written, ISC-122..129 shipped per-bead cost/usage/model telemetry lifted from
the envelope the CLI was already returning. Every production cycle is now an
eval sample, so the harness is redundant for the first wave — the data arrives
free, from real workloads rather than proxies for them.

What the harness would still have supplied, and what this module adds, is the
DISCIPLINE around reading it.

THE INFORMATIVENESS GATE
------------------------
DAPO's dynamic-sampling result: a group where every sample succeeds, or every
sample fails, has zero advantage after normalisation and contributes nothing to
the gradient. Sampling more of it is wasted compute.

The same holds for routing. A task type where every model completes everything
tells you nothing about which model is *better* — though it still tells you
which is cheaper. A task type where every model fails tells you nothing at all,
and "route to the cheapest failure" is worse than silence. So every group is
classified before it is allowed to produce a recommendation:

  insufficient  - too few runs to say anything (the honest default)
  single_arm    - only one model observed; nothing to compare against
  all_failing   - no model completes anything; fix the work, not the routing
  cost_only     - all arms succeed equally; quality is uninformative, cost is not
  informative   - success rates differ; rank by cost per COMPLETED bead

Only ``cost_only`` and ``informative`` yield a recommendation. This is the
module's whole point: a routing table that reports "insufficient" for a year is
correct, and one that confidently recommends from four samples is not.

Everything here is READ-ONLY and advisory. Nothing mutates config, and nothing
auto-routes — that is deliberately deferred, matching the triage/notify
degradation contract where an advisory subsystem never fails a cycle.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .children import ChildRegistry
from .config import Config
from .cost import read_ledger

# Below this many runs, a cell is noise. Deliberately conservative: the live
# ledger holds ~62 records total, and ISC-150 already establishes the house
# rule that a correction built on under-3 samples is fiction.
DEFAULT_MIN_SAMPLES = 5
# An arm needs its own floor before it can be compared to another arm.
DEFAULT_MIN_ARM_SAMPLES = 3

UNINFORMATIVE = ("insufficient", "single_arm", "all_failing")


@dataclass
class Arm:
    """One model's record within a group."""

    model: str
    runs: int = 0
    completed: int = 0
    cost_usd: float = 0.0
    priced_runs: int = 0
    seconds: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.completed / self.runs if self.runs else 0.0

    @property
    def cost_per_completed(self) -> Optional[float]:
        """None, never 0.0 — an unknown cost must not render as a free one."""
        if not self.completed or not self.priced_runs:
            return None
        return self.cost_usd / self.completed

    @property
    def avg_seconds(self) -> Optional[float]:
        return self.seconds / self.runs if self.runs else None


@dataclass
class Group:
    """All arms observed for one task type (or whatever dimension was grouped)."""

    key: str
    arms: dict[str, Arm] = field(default_factory=dict)

    @property
    def runs(self) -> int:
        return sum(a.runs for a in self.arms.values())

    @property
    def completed(self) -> int:
        return sum(a.completed for a in self.arms.values())


@dataclass
class Verdict:
    kind: str
    reason: str
    recommended_model: Optional[str] = None
    # Populated only when a recommendation was actually made.
    basis: Optional[str] = None


def _entry_model(e: dict) -> str:
    """The model that ACTUALLY authored the answer, falling back honestly.

    ISC-128 established that ``model_used`` is the executing model rather than
    the requested one. When the envelope carried neither, the arm is named
    ``(unknown)`` rather than being silently folded into a real model's stats.
    """
    for k in ("model_used", "requested_model"):
        v = e.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "(unknown)"


def build_groups(entries: Iterable[dict], group_by: str = "task_type") -> dict[str, Group]:
    """Bucket ledger entries into groups x arms."""
    groups: dict[str, Group] = {}
    for e in entries:
        raw = e.get(group_by)
        key = raw.strip() if isinstance(raw, str) and raw.strip() else "(unset)"
        g = groups.setdefault(key, Group(key=key))
        model = _entry_model(e)
        arm = g.arms.setdefault(model, Arm(model=model))
        arm.runs += 1
        if e.get("success"):
            arm.completed += 1
        cost = e.get("cost_usd")
        if isinstance(cost, (int, float)):
            arm.cost_usd += float(cost)
            arm.priced_runs += 1
        secs = e.get("duration_seconds")
        if isinstance(secs, (int, float)):
            arm.seconds += float(secs)
    return groups


def classify(
    group: Group,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_arm_samples: int = DEFAULT_MIN_ARM_SAMPLES,
) -> Verdict:
    """Decide whether this group carries a routing signal at all.

    The gate runs BEFORE any ranking, so an uninformative group can never leak
    a recommendation by way of a tie-break.
    """
    if group.runs < min_samples:
        return Verdict(
            "insufficient",
            f"{group.runs} run(s), need {min_samples}",
        )

    comparable = [a for a in group.arms.values() if a.runs >= min_arm_samples]
    if len(comparable) < 2:
        n = len(comparable)
        return Verdict(
            "single_arm",
            f"{n} model(s) with >= {min_arm_samples} runs — nothing to compare against",
        )

    if all(a.completed == 0 for a in comparable):
        return Verdict(
            "all_failing",
            "no model completes anything here — this is a work problem, not a routing one",
        )

    rates = {round(a.success_rate, 6) for a in comparable}
    priced = [a for a in comparable if a.cost_per_completed is not None]

    if len(rates) == 1:
        # Quality is uninformative (DAPO's all-equal group). Cost still is —
        # but only if some arm actually carried a price.
        if not priced:
            return Verdict(
                "insufficient",
                "all arms tie on success and none carried a price — no dimension discriminates",
            )
        best = min(priced, key=lambda a: a.cost_per_completed)  # type: ignore[arg-type]
        return Verdict(
            "cost_only",
            f"all {len(comparable)} arms succeed at {comparable[0].success_rate:.0%} — "
            "quality carries no signal, ranking on cost alone",
            recommended_model=best.model,
            basis="cost_per_completed",
        )

    # Success rates differ: rank on cost per completed bead, which already
    # charges failures to the successes (ISC-125).
    if not priced:
        best = max(comparable, key=lambda a: a.success_rate)
        return Verdict(
            "informative",
            "success rates differ but no arm carried a price — ranking on success rate alone",
            recommended_model=best.model,
            basis="success_rate",
        )
    best = min(priced, key=lambda a: a.cost_per_completed)  # type: ignore[arg-type]
    return Verdict(
        "informative",
        f"success rates differ across {len(comparable)} arms",
        recommended_model=best.model,
        basis="cost_per_completed",
    )


@dataclass
class LedgerHealth:
    """What kind of comparison the ledger can currently support.

    Deliberately two separate questions, because they have different answers
    and collapsing them produces a report that contradicts itself:

    - can_compare_quality: did any run ever FAIL? Without both outcomes present
      there is no gradient to rank models by (DAPO's all-equal group).
    - can_compare_cost: are there >= 2 priced arms? Cost discriminates even when
      quality does not, and "everyone succeeds, this one is cheaper" is a
      legitimate — if weaker — routing basis.
    """

    total_runs: int
    distinct_models: int
    distinct_groups: int
    outcome_variance: bool  # did anything ever both succeed AND fail?
    priced_runs: int

    @property
    def can_compare_quality(self) -> bool:
        return self.total_runs > 0 and self.distinct_models >= 2 and self.outcome_variance

    @property
    def can_compare_cost(self) -> bool:
        return self.total_runs > 0 and self.distinct_models >= 2 and self.priced_runs > 0


def assess_ledger(entries: list[dict], group_by: str = "task_type") -> LedgerHealth:
    successes = {bool(e.get("success")) for e in entries}
    groups = build_groups(entries, group_by)
    return LedgerHealth(
        total_runs=len(entries),
        distinct_models=len({_entry_model(e) for e in entries}),
        distinct_groups=len(groups),
        outcome_variance=len(successes) > 1,
        priced_runs=sum(1 for e in entries if isinstance(e.get("cost_usd"), (int, float))),
    )


def portfolio_ledger(
    config_path: str, _seen: Optional[set[str]] = None
) -> list[dict]:
    """Every cost entry across this instance and all registered children.

    Read-only, with the same walk discipline as ``me.collect`` and
    ``me.portfolio_tasks``: registry cycles are cut with a resolved-path seen
    set, and an unreadable child is skipped with a warning that names the
    consequence rather than raising. A node that cannot be read makes the
    evidence base THINNER, which pushes verdicts toward "insufficient" — the
    safe direction for a skipped input to err.
    """
    seen = _seen if _seen is not None else set()
    real = str(Path(config_path).resolve())
    if real in seen:
        return []
    seen.add(real)

    try:
        config = Config.load(config_path)
        entries = list(read_ledger(config.tasks_path))
    except Exception as e:  # noqa: BLE001 — one broken node must not hide the rest
        print(
            f"[eval] WARNING: could not read {config_path} ({e}) — its runs are "
            f"missing from the routing evidence, which is now thinner",
            file=sys.stderr,
        )
        return []

    for entry in entries:
        entry.setdefault("_node", real)

    registry = ChildRegistry(config.children_registry_path)
    for child in registry.list():
        child_config = Path(child.path) / "config.yaml" if child.path else None
        if child_config and child_config.is_file():
            entries.extend(portfolio_ledger(str(child_config), _seen=seen))
    return entries


def evaluate(
    entries: list[dict],
    group_by: str = "task_type",
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_arm_samples: int = DEFAULT_MIN_ARM_SAMPLES,
) -> tuple[LedgerHealth, list[tuple[Group, Verdict]]]:
    health = assess_ledger(entries, group_by)
    groups = build_groups(entries, group_by)
    out = [
        (g, classify(g, min_samples, min_arm_samples))
        for g in sorted(groups.values(), key=lambda g: -g.runs)
    ]
    return health, out


def recommendations(results: list[tuple[Group, Verdict]]) -> dict[str, str]:
    """group key -> recommended model, for groups that earned a recommendation."""
    return {
        g.key: v.recommended_model
        for g, v in results
        if v.recommended_model and v.kind not in UNINFORMATIVE
    }


def format_report(
    health: LedgerHealth,
    results: list[tuple[Group, Verdict]],
    group_by: str,
    min_samples: int,
) -> str:
    L: list[str] = []
    L.append(f"Routing evidence — grouped by {group_by}")
    L.append("=" * 66)
    L.append(f"runs observed   : {health.total_runs}")
    L.append(f"distinct models : {health.distinct_models}")
    L.append(f"priced runs     : {health.priced_runs}")
    L.append(f"outcome variance: {'yes' if health.outcome_variance else 'NO — every run has the same outcome'}")
    L.append("")

    if not health.total_runs:
        L.append("No telemetry yet. Runs are recorded as beads execute; check back after a cycle.")
        return "\n".join(L)

    if health.distinct_models < 2:
        L.append("NO COMPARISON IS POSSIBLE YET.")
        L.append(f"  Only {health.distinct_models} model observed; routing needs at least two arms.")
        L.append("")
    elif not health.can_compare_quality:
        L.append("QUALITY CANNOT BE COMPARED — cost-only evidence.")
        L.append("  Every recorded run has the same outcome, so no ranking can discriminate on")
        L.append("  quality. A group with zero outcome variance carries no gradient (DAPO's")
        L.append("  dynamic sampling): sampling more of it is wasted compute, not more evidence.")
        L.append("")
        L.append("  Recommendations below therefore rest on COST ALONE, and rest on an")
        L.append("  assumption the ledger cannot check: `success` records that a bead")
        L.append("  COMPLETED, not that its output was good. Two models that both finish are")
        L.append("  equal here even if one did visibly worse work.")
        L.append("")

    head = f"{'model':<24} {'runs':>5} {'ok':>4} {'ok%':>5} {'$/done':>9} {'avg s':>7}"
    for g, v in results:
        L.append(f"[{g.key}]  {g.runs} run(s) — {v.kind.upper()}: {v.reason}")
        L.append("  " + head)
        L.append("  " + "-" * len(head))
        for arm in sorted(g.arms.values(), key=lambda a: -a.runs):
            cpc = arm.cost_per_completed
            L.append(
                f"  {arm.model[:24]:<24} {arm.runs:>5} {arm.completed:>4} "
                f"{arm.success_rate * 100:>4.0f}% "
                f"{(f'{cpc:.4f}' if cpc is not None else '—'):>9} "
                f"{(f'{arm.avg_seconds:.1f}' if arm.avg_seconds is not None else '—'):>7}"
            )
        if v.recommended_model:
            L.append(f"  -> route to {v.recommended_model}  (by {v.basis})")
        L.append("")

    recs = recommendations(results)
    L.append("-" * 66)
    if recs:
        L.append(f"{len(recs)} group(s) carry enough evidence to recommend:")
        for k, m in recs.items():
            L.append(f"  {k}: {m}")
        L.append("")
        L.append("ADVISORY ONLY. Nothing here changes config; `agentco doctor` will")
        L.append("warn where a configured model disagrees with the evidence.")
    else:
        L.append("No group carries enough evidence to recommend a model.")
        L.append(f"Groups need >= {min_samples} runs across >= 2 comparable models.")
        L.append("This is the correct output for a thin ledger — let it accumulate.")
    return "\n".join(L)


def to_json(
    health: LedgerHealth, results: list[tuple[Group, Verdict]]
) -> dict[str, Any]:
    return {
        "health": {
            "total_runs": health.total_runs,
            "distinct_models": health.distinct_models,
            "distinct_groups": health.distinct_groups,
            "outcome_variance": health.outcome_variance,
            "priced_runs": health.priced_runs,
            "can_compare_quality": health.can_compare_quality,
            "can_compare_cost": health.can_compare_cost,
        },
        "groups": [
            {
                "key": g.key,
                "runs": g.runs,
                "completed": g.completed,
                "verdict": v.kind,
                "reason": v.reason,
                "recommended_model": v.recommended_model,
                "basis": v.basis,
                "arms": [
                    {
                        "model": a.model,
                        "runs": a.runs,
                        "completed": a.completed,
                        "success_rate": round(a.success_rate, 4),
                        "cost_usd": round(a.cost_usd, 4),
                        "cost_per_completed": (
                            round(a.cost_per_completed, 4)
                            if a.cost_per_completed is not None
                            else None
                        ),
                        "priced_runs": a.priced_runs,
                    }
                    for a in sorted(g.arms.values(), key=lambda a: -a.runs)
                ],
            }
            for g, v in results
        ],
        "recommendations": recommendations(results),
    }
