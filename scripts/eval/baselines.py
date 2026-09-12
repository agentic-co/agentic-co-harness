#!/usr/bin/env python3
"""Recompute published tau2-bench airline baselines on OUR task subsets.

    python3 scripts/eval/baselines.py --tau2 <checkout>

Why recompute rather than quote the leaderboard: the published runs score on
`['DB', 'COMMUNICATE']`, where COMMUNICATE is judged by a model. Our arms score
DB only, deliberately — an experiment about whether procedures improve
verification should not rest its verdict on a judge. Quoting their headline
number next to ours would compare a stricter score to a looser one and flatter
us by roughly the difference.

Their raw simulations carry `reward_info.db_check.db_reward`, so the DB-only
figure is recoverable exactly, on whichever tasks we choose.

These are a REFERENCE LINE, not a like-for-like comparison: different models,
different trial counts, and they were not run through a gated procedure. Any
write-up that puts them in one table has to say so.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tau2", type=Path, required=True)
    ap.add_argument("--data", type=Path, default=Path("evals/tau2-airline-asop"))
    ap.add_argument("--subset", nargs="*", default=["16", "11", "32", "33"])
    args = ap.parse_args()

    split = json.loads((args.data / "split.json").read_text())
    test = set(map(str, split["test"]))
    sub = set(map(str, args.subset))

    files = sorted(glob.glob(str(args.tau2 / "data/tau2/results/final/*airline*.json")))
    if not files:
        raise SystemExit("no shipped airline results found in the checkout")

    print(f"{'model':<34}{'full 50':>10}{'TEST':>8}{'subset':>8}   trials")
    print("-" * 70)
    for f in files:
        d = json.loads(Path(f).read_text())
        name = os.path.basename(f).split("_airline")[0]
        rows = [
            (str(s["task_id"]), s["reward_info"]["db_check"]["db_reward"])
            for s in d["simulations"]
        ]
        allr = [r for _, r in rows]
        tst = [r for t, r in rows if t in test]
        s4 = [r for t, r in rows if t in sub]
        trials = len(allr) // len({t for t, _ in rows})
        print(
            f"{name:<34}{sum(allr) / len(allr):>10.3f}"
            f"{(sum(tst) / len(tst) if tst else float('nan')):>8.3f}"
            f"{(sum(s4) / len(s4) if s4 else float('nan')):>8.3f}   {trials}"
        )
    print("\nDB-only. A reference line, not a like-for-like comparison.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
