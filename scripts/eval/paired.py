#!/usr/bin/env python3
"""Paired comparison of two tau2 arms over their matched cells.

Every arm comparison in this programme has been done by hand, and twice it went
wrong in the same way: the two arms excluded DIFFERENT unscoreable cells, so the
headline pass^1 figures were computed over different denominators and quoted
against each other anyway. (Airline arm B 18/26 against arm E 18/24 is the live
example.) This script refuses that mistake by construction -- it scores both arms
only over the cells BOTH arms scored, and reports the paired sign test on top.

A cell is (task_id, trial). Two kinds of cell are dropped, and the counts are
reported rather than hidden, because a comparison that silently drops six cells
is not the comparison you think you are reading:

  * reward absent or non-numeric;
  * `harness_fixes.unscoreable_cells` FAULT 2 -- the user granted consent and
    stopped in the same turn, so the agent never got a turn in which to act.

The second is dropped on the UNION across both arms, not per-arm. The runners
exclude it per-arm, which is right for a single arm's headline number and wrong
for a pairing: it leaves the two arms scored over different cell sets, which is
exactly how 18/26 came to be quoted against 18/24. Here, a cell unscoreable in
EITHER arm is dropped from BOTH, so the comparison is over one common set.

The p-value is the exact two-sided sign test over discordant pairs, which is what
this project has quoted throughout (arm B vs C, p = 0.039). It makes no normal
approximation, so it stays honest at the n=9 discordant pairs these runs produce.

    python3 scripts/eval/paired.py A_LABEL A_DIR B_LABEL B_DIR [...]

Regime warning: --defer-consent-stop changes when conversations END, so a run
with it is not comparable to one without. This script cannot detect that from
results.json -- the caller must not pair across regimes. It prints the reminder.
"""

from __future__ import annotations

import json
import sys
from math import comb
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_fixes  # noqa: E402  (same directory, no package)


def _as_objects(results: dict) -> SimpleNamespace:
    """Adapt parsed JSON to the attribute access `harness_fixes` expects.

    `unscoreable_cells` reads a live tau2 results object. Re-implementing its
    consent-and-stop rule here would give this estate two detectors that must
    never disagree, which is the defect the whole unification plan exists to
    remove -- so adapt the data to the one detector instead of forking it.
    """
    def wrap(value):
        if isinstance(value, dict):
            return SimpleNamespace(**{k: wrap(v) for k, v in value.items()})
        if isinstance(value, list):
            return [wrap(v) for v in value]
        return value

    return SimpleNamespace(simulations=[wrap(s) for s in results["simulations"]])


def load(run_dir: Path) -> tuple[dict[tuple[str, int], float], set[tuple[str, int]]]:
    """Return (scoreable rewards by cell, cells hit by harness FAULT 2)."""
    results = json.loads((run_dir / "results.json").read_text())
    sims = results["simulations"] if isinstance(results, dict) else results
    cells: dict[tuple[str, int], float] = {}
    for sim in sims:
        info = sim.get("reward_info") or {}
        reward = info.get("reward")
        if not isinstance(reward, (int, float)):
            continue  # no reward at all: dropped, never counted as a zero
        cells[(str(sim["task_id"]), int(sim["trial"]))] = float(reward)
    faulted = {
        (str(c["task_id"]), int(c["trial"]))
        for c in harness_fixes.unscoreable_cells(_as_objects(results))
    }
    return cells, faulted


def sign_test(wins: int, losses: int) -> float:
    """Exact two-sided sign test. Ties carry no information and are excluded."""
    n = wins + losses
    if n == 0:
        return 1.0
    tail = sum(comb(n, k) for k in range(0, min(wins, losses) + 1))
    return min(1.0, 2 * tail / 2**n)


def compare(a_label: str, a_dir: Path, b_label: str, b_dir: Path) -> None:
    (a, a_faults), (b, b_faults) = load(a_dir), load(b_dir)
    faulted = a_faults | b_faults
    common = sorted((set(a) & set(b)) - faulted)
    dropped_a, dropped_b = sorted(set(b) - set(a)), sorted(set(a) - set(b))

    a_pass = sum(1 for c in common if a[c] >= 1.0)
    b_pass = sum(1 for c in common if b[c] >= 1.0)
    wins = sum(1 for c in common if a[c] > b[c])
    losses = sum(1 for c in common if a[c] < b[c])
    ties = len(common) - wins - losses
    n = len(common)

    print(f"\n{a_label}  vs  {b_label}")
    print(f"  matched cells      {n}"
          f"   (no reward in {a_label}: {len(dropped_a)},"
          f" in {b_label}: {len(dropped_b)};"
          f" consent-stop fault dropped from both: {len(faulted)})")
    if n == 0:
        print("  NO MATCHED CELLS -- different task sets, nothing to compare.")
        return
    print(f"  {a_label:<22} pass^1 = {a_pass / n:.3f}  ({a_pass}/{n})")
    print(f"  {b_label:<22} pass^1 = {b_pass / n:.3f}  ({b_pass}/{n})")
    print(f"  delta              {(a_pass - b_pass) / n:+.3f}")
    print(f"  paired             {a_label} wins {wins}, {b_label} wins {losses},"
          f" tied {ties}")
    print(f"  exact two-sided p  {sign_test(wins, losses):.3f}"
          f"   (sign test over {wins + losses} discordant pairs)")
    if wins + losses < 6:
        print("  ^ fewer than 6 discordant pairs: p cannot reach 0.05 whatever "
              "the direction. Report the direction, not significance.")
    if dropped_a or dropped_b:
        disagree = sorted(set(dropped_a) | set(dropped_b))[:6]
        print(f"  dropped cells      {disagree}{' ...' if len(disagree) == 6 else ''}")


def main(argv: list[str]) -> int:
    if len(argv) < 4 or len(argv) % 4 != 0:
        print(__doc__)
        return 2
    print("REGIME REMINDER: never pair a --defer-consent-stop run against one "
          "without it.\nThe runs' own rewards cannot reveal the mismatch; only "
          "the invocation can.")
    for i in range(0, len(argv), 4):
        a_label, a_dir, b_label, b_dir = argv[i:i + 4]
        compare(a_label, Path(a_dir), b_label, Path(b_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
