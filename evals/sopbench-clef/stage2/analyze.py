#!/usr/bin/env python3
"""Stage 2 readout: does a Clef value gate change hotel outcomes vs A3 / A4-Jev?

    ASOP_SCRATCH=evals/sopbench-clef/stage2/score-layout ASOP_DOMAIN=hotel \\
    ASOP_MODEL=glm-4.7 SOPBENCH_HOME=~/Code/sopbench \\
        ~/Code/sopbench/.venv/bin/python evals/sopbench-clef/stage2/analyze.py [--json out.json]

Scores the three stage-2 arms with `score_asop_arms.score_file` (SOPBench's own
evaluator), restricts to the tasks ALL three have on disk (so it is safe to run
mid-flight: an interim readout, not a final one, until each arm hits 195), and
pairs them with the scorer's own exact McNemar.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "evals" / "sopbench-bank-asop"))
import score_asop_arms as s  # noqa: E402

ARMS = {"A3 toolgate": "asop-v2-toolgate", "A4 jev": "asop-v2-jev", "A4c clef": "asop-v2-clef"}
PAIRS = [("A4c clef", "A3 toolgate"), ("A4c clef", "A4 jev"), ("A4 jev", "A3 toolgate")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    arms = {label: s.score_file(s.ARM_PATHS[key]) for label, key in ARMS.items()}
    keys = sorted(set.intersection(*(set(v) for v in arms.values())), key=lambda k: k[1])
    n = len(keys)
    out = {"n": n, "per_arm_on_disk": {k: len(v) for k, v in arms.items()}, "rates": {}, "pairs": {}}
    print(f"hotel stage 2 — n={n} common tasks (on disk: {out['per_arm_on_disk']})\n")
    for label, v in arms.items():
        r = {m: sum(v[k][m] for k in keys) / n for m in ("success",)}
        # Split by whether the task's goal action SHOULD succeed: a value gate
        # earns its keep on should-refuse tasks and pays on should-succeed ones.
        for want in (True, False):
            sub = [k for k in keys if v[k]["action_should_succeed"] is want]
            r[f"success|should_{'succeed' if want else 'refuse'}"] = (
                sum(v[k]["success"] for k in sub) / len(sub) if sub else None)
            r[f"n|should_{'succeed' if want else 'refuse'}"] = len(sub)
        out["rates"][label] = r
        print(f"{label:<12} success {r['success']:.3f}   should-succeed {r['success|should_succeed']:.3f}"
              f" (n={r['n|should_succeed']})   should-refuse {r['success|should_refuse']:.3f} (n={r['n|should_refuse']})")
    print()
    for a, b in PAIRS:
        p = s.paired(arms, keys, a, base=b)
        out["pairs"][f"{a} vs {b}"] = p
        print(f"{a} vs {b}: Δ {p['delta']:+.3f} [{p['lo']:+.3f},{p['hi']:+.3f}]  "
              f"gained {p['gained']} lost {p['lost']}  McNemar p={p['p']:.3f}")
    if args.json:
        args.json.write_text(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
