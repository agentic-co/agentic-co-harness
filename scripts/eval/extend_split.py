#!/usr/bin/env python3
"""Enlarge a domain's TEST split so a paired arm can actually reach significance.

    uv run --with "tau2 @ file://$HOME/Code/tau2-bench" \
      python scripts/eval/extend_split.py \
        --tau2 ~/Code/tau2-bench --domain retail \
        --data evals/tau2-retail-asop --size 40

WHY. `EVIDENCE.md` §4: retail's discordant rate is ~14%, so an 18-task × 2-trial
arm (36 cells) yields ~5 discordant pairs, and **6 all-one-way is the minimum
that can clear p<0.05**. A 36-cell arm therefore CANNOT produce a significant
result whichever way it points, and four numbers from this design have already
been withdrawn after being quoted as though they meant something. 40 tasks × 4
trials ≈ 160 cells ⇒ ~22 discordant, which is resolvable.

TWO PROPERTIES THIS PRESERVES, and they are the reason this is a script rather
than a one-off shuffle:

1. **DEV is never drawn from.** DEV is where failures get adjudicated and where
   revisions are drafted. A task that informed a revision cannot also measure
   it — re-running the tasks you learned from measures leakage and calls it
   improvement. `taubench.py`'s own docstring says exactly this.

2. **The existing TEST is kept whole, as a prefix.** The 22 new tasks are added
   to the 18 already there rather than a fresh 40 being redrawn. So the enlarged
   arm contains the old arm: the overlapping 18 stay comparable to historical
   runs, and the growth is additive rather than a regime change nobody declared.

⚠️ WHAT IT DOES NOT PRESERVE. An arm run on TEST-40 is **not** comparable to a
historical 36-cell arm — different task set, different difficulty mix. Only arms
run on the SAME split may be paired, and `paired.py` remains the only sanctioned
comparison. Running a fresh stock arm alongside is not optional.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tau2", type=Path, required=True)
    ap.add_argument("--domain", default="retail")
    ap.add_argument("--data", type=Path, required=True, help="dir holding split.json")
    ap.add_argument("--size", type=int, default=40, help="target TEST size")
    ap.add_argument("--seed", type=int, default=20260916)
    ap.add_argument("--out", type=Path, default=None,
                    help="default: <data>/split.test%(size)d.json")
    a = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import taubench

    taubench.set_domain(a.domain)
    gradeable, read_only = taubench.discriminating(a.tau2)

    split = json.loads((a.data / "split.json").read_text())
    dev, test = list(split["dev"]), list(split["test"])

    # The reproducibility check comes FIRST and is fatal. If the gradeable set
    # does not come back the same, the recorded split was drawn from a different
    # population and nothing built on top of it means what it says.
    if len(gradeable) != split["gradeable_tasks"]:
        print(f"REFUSING: gradeable set does not reproduce — recorded "
              f"{split['gradeable_tasks']}, got {len(gradeable)}. The existing split "
              f"was drawn from a different population; do not extend it.", file=sys.stderr)
        return 1
    pool = set(gradeable)
    if not (set(dev) <= pool and set(test) <= pool):
        print("REFUSING: the recorded DEV/TEST are not subsets of the gradeable set.",
              file=sys.stderr)
        return 1

    available = sorted(pool - set(dev) - set(test), key=int)
    need = a.size - len(test)
    if need < 0:
        print(f"TEST is already {len(test)}; --size {a.size} would SHRINK it. Refusing.",
              file=sys.stderr)
        return 1
    if need > len(available):
        print(f"REFUSING: need {need} more tasks, only {len(available)} available "
              f"outside DEV and TEST.", file=sys.stderr)
        return 1

    rng = random.Random(a.seed)
    extra = sorted(rng.sample(available, need), key=int)
    test40 = test + extra

    assert not (set(test40) & set(dev)), "DEV leaked into TEST"
    assert len(set(test40)) == len(test40), "duplicate task in TEST"
    assert test40[:len(test)] == test, "the original TEST must survive as a prefix"

    out = a.out or (a.data / f"split.test{a.size}.json")
    out.write_text(json.dumps({
        "domain": a.domain,
        "derived_from": "split.json",
        "base_seed": split["seed"],
        "extension_seed": a.seed,
        "gradeable_tasks": len(gradeable),
        "read_only_excluded": len(read_only),
        "dev": dev,
        "test": test40,
        "test_original": test,
        "test_added": extra,
        "scoring": split.get("scoring"),
        "note": (
            "TEST enlarged so a paired arm can reach p<0.05 (EVIDENCE.md §4). DEV is "
            "untouched and was never drawn from. The original 18 TEST tasks are kept as "
            "a prefix. An arm on THIS split is not comparable to a historical 36-cell "
            "arm; run a matched stock arm on the same split and compare with paired.py."
        ),
    }, indent=2))

    print(f"[extend_split] {a.domain}: gradeable={len(gradeable)} dev={len(dev)} "
          f"TEST {len(test)} -> {len(test40)} (+{len(extra)}), seed {a.seed}")
    print(f"  kept:  {' '.join(test)}")
    print(f"  added: {' '.join(extra)}")
    print(f"  wrote: {out}")
    print(f"\n  cells at 4 trials: {len(test40) * 4} per arm, two arms = {len(test40) * 8} sims")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
