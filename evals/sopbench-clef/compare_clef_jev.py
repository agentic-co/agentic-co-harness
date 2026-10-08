#!/usr/bin/env python3
"""Clef vs Jev (and GLM-4.7 for context) on every SOPBench cell scored so far.

    python3 evals/sopbench-clef/compare_clef_jev.py [--json out.json]

Pairs Clef's verdicts against Jev's published verdicts on the identical
decision ids via `judge_paired_bootstrap.compare` (A = Jev, B = Clef, so a
positive delta means Clef is better on lift/TPR). Cells Clef has not reached
yet are skipped, not zero-filled. Also reports the repeat-run flip rate on
`hotel`/`university`, whose v1 and v2 evidence is byte-identical.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "eval"))
from judge_paired_bootstrap import compare  # noqa: E402

DOMAINS = ["bank", "online_market", "hotel", "library", "healthcare", "dmv", "university"]


def ref_dir(domain: str, render: str) -> Path:
    base = ROOT / "evals" / f"sopbench-{domain.replace('_', '-')}-asop"
    # bank v1 predates the v1/ subdirectory convention.
    return base if (domain, render) == ("bank", "v1") else base / render


def lift(path: Path) -> float | None:
    return json.loads(path.read_text())["summary"]["lift"] if path.exists() else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()

    rows = []
    print(f"{'cell':<20}{'n':>5}  {'Jev':>7}{'Clef':>7}{'GLM':>7}   {'Δlift (Clef−Jev)':<28}{'ΔTPR':>8}{'ΔFPR':>8}  errs")
    for render in ("v1", "v2"):
        for d in DOMAINS:
            clef = ROOT / "evals" / "sopbench-clef" / d / render / "sopbench_jev_clef.json"
            if not clef.exists():
                continue
            jev = ref_dir(d, render) / "sopbench_jev_jev-latest.json"
            glm = ref_dir(d, render) / "sopbench_zai_glm-4-7.json"
            res = compare(jev, clef, f"{d} {render}", reps=10000, seed=20260923)
            dl, dt, df = (res["delta"][k] for k in ("lift", "TPR", "FPR"))
            errs = json.loads(clef.read_text())["summary"]["errors"]
            sig = "*" if (dl["ci95"][0] > 0 or dl["ci95"][1] < 0) else " "
            print(f"{d + ' ' + render:<20}{res['n']:>5}  {res['a']['lift']:>+7.3f}{res['b']['lift']:>+7.3f}"
                  f"{(lift(glm) or float('nan')):>+7.3f}   {dl['point']:>+.3f} [{dl['ci95'][0]:+.3f},{dl['ci95'][1]:+.3f}]{sig}"
                  f"{'':<4}{dt['point']:>+8.3f}{df['point']:>+8.3f}  {errs}")
            rows.append({**res, "glm_lift": lift(glm), "clef_errors": errs})

    # Repeat-run determinism: same evidence twice.
    for d in ("hotel", "university"):
        a = ROOT / "evals" / "sopbench-clef" / d / "v1" / "sopbench_jev_clef.json"
        b = ROOT / "evals" / "sopbench-clef" / d / "v2" / "sopbench_jev_clef.json"
        if a.exists() and b.exists():
            va = {x["id"]: x["passed"] for x in json.loads(a.read_text())["decisions"]}
            vb = {x["id"]: x["passed"] for x in json.loads(b.read_text())["decisions"]}
            flips = sum(1 for i in va if va[i] != vb.get(i))
            print(f"determinism {d}: {flips}/{len(va)} verdicts flipped on identical evidence")

    if args.json:
        args.json.write_text(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
