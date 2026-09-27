#!/usr/bin/env python3
"""Paired comparison of one judge across two renderings of the SAME decisions.

    python3 scripts/eval/judge_paired_bootstrap.py \\
        --a v1/sopbench_zai_glm-4-7.json --b v2/sopbench_zai_glm-4-7.json \\
        --label "GLM-4.7 bank"

WHY A PAIRED TEST AND NOT TWO CIs
---------------------------------
`sopbench_judge.score()` publishes a Wald interval per arm, which answers "is
this judge better than chance". It does not answer "did the revision change
anything", because the two arms are the *same decisions* — the arm-level
intervals share almost all of their sampling noise and comparing them by
overlap is both wrong and conservative. The decisions are matched by `_id`, so
the comparison is paired and the resampling has to be too.

This is the same shape as the Laya evidence-ordering correction
(`LAYA-JUDGE.md`: "+0.235, P(Δ≤0) = 0.0000"), and it reuses that recipe rather
than inventing a second one.

METHOD
------
Resample **decision ids with replacement, stratified within truth class**, so
every bootstrap replicate keeps the 200/200 balanced design the sample was
drawn with — LIFT is Youden's J and the design is balanced on purpose, so
resampling across classes would let the class sizes wander and put variance
into the statistic that the experiment does not have. Both arms are re-scored
on the *same* resampled ids, which is what makes it paired.

Reported per statistic (lift, TPR, FPR): the point estimate for each arm, the
delta, a percentile bootstrap CI on the delta, and the one-sided bootstrap
probability that the delta falls on the null side.

⚠️ **TPR and FPR are reported with equal prominence, deliberately.** A
rendering change can always buy FPR by asking the judge for less, and the
resulting lift can still look flat or positive. The only thing that
distinguishes a real fix from a weakened check is whether TPR held, so this
tool never prints a lift delta without the TPR delta beside it.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path


def load(path: Path) -> dict[str, bool | None]:
    """{decision id: judge said PASS}. Unparseable verdicts stay None."""
    blob = json.loads(Path(path).read_text())
    return {d["id"]: d["passed"] for d in blob["decisions"]}


def truths(path: Path) -> dict[str, str]:
    blob = json.loads(Path(path).read_text())
    return {d["id"]: d["truth"] for d in blob["decisions"]}


def rates(ids: list[str], verdicts: dict[str, bool | None],
          truth: dict[str, str]) -> tuple[float | None, float | None, float | None]:
    """(TPR, FPR, lift) over `ids`, skipping decisions with no verdict."""
    tp = fp = n_nh = n_h = 0
    for i in ids:
        v = verdicts.get(i)
        if v is None:
            continue
        if truth[i] == "not_held":
            n_nh += 1
            tp += v is False          # refused a genuine violation
        else:
            n_h += 1
            fp += v is False          # refused correct work
    if not n_nh or not n_h:
        return None, None, None
    tpr, fpr = tp / n_nh, fp / n_h
    return tpr, fpr, tpr - fpr


def compare(a_path: Path, b_path: Path, label: str, reps: int, seed: int) -> dict:
    va, vb = load(a_path), load(b_path)
    truth = truths(a_path)
    if set(va) != set(vb):
        raise SystemExit(
            f"the two arms do not cover the same decisions "
            f"({len(set(va) - set(vb))} only in A, {len(set(vb) - set(va))} only in B) "
            f"— a paired comparison over a shifted id set is not a comparison")
    if truths(b_path) != truth:
        raise SystemExit("the two arms disagree about ground truth — refusing to pair")

    ids = sorted(truth)
    nh = [i for i in ids if truth[i] == "not_held"]
    h = [i for i in ids if truth[i] == "held"]

    point = {}
    for name, v in (("a", va), ("b", vb)):
        tpr, fpr, lift = rates(ids, v, truth)
        point[name] = {"TPR": tpr, "FPR": fpr, "lift": lift,
                       "unscored": sum(1 for i in ids if v[i] is None)}

    rnd = random.Random(seed)
    draws: dict[str, list[float]] = {"lift": [], "TPR": [], "FPR": []}
    for _ in range(reps):
        # Stratified within class: the 200/200 design is held fixed.
        pick = ([rnd.choice(nh) for _ in nh] + [rnd.choice(h) for _ in h])
        ta, fa, la = rates(pick, va, truth)
        tb, fb, lb = rates(pick, vb, truth)
        if la is None or lb is None:
            continue
        draws["lift"].append(lb - la)
        draws["TPR"].append(tb - ta)
        draws["FPR"].append(fb - fa)

    out = {"label": label, "n": len(ids), "n_not_held": len(nh), "n_held": len(h),
           "reps": reps, "seed": seed, "a": point["a"], "b": point["b"], "delta": {}}
    for stat, ds in draws.items():
        ds_sorted = sorted(ds)
        k = len(ds_sorted)
        delta = point["b"][stat] - point["a"][stat]
        # One-sided bootstrap probabilities. For lift and TPR the null side is
        # "the revision did not help / did harm"; for FPR the improvement is
        # negative, so the null side is reported as P(Δ >= 0).
        p_le0 = sum(1 for d in ds_sorted if d <= 0) / k if k else None
        p_ge0 = sum(1 for d in ds_sorted if d >= 0) / k if k else None
        out["delta"][stat] = {
            "point": delta,
            "ci95": [ds_sorted[int(0.025 * k)], ds_sorted[int(0.975 * k) - 1]] if k else None,
            "P(delta<=0)": p_le0,
            "P(delta>=0)": p_ge0,
        }

    # The guard, computed rather than eyeballed: a revision that buys FPR by
    # refusing less of everything shows up as TPR falling alongside it.
    #
    # ⚠️ Reported as TWO facts, not one boolean, because either alone misleads.
    # A strict `delta >= 0` test calls a -0.010 point estimate a guard failure
    # when its interval is [-0.060, +0.040] and it is plainly noise; a
    # significance test alone lets a real drop hide behind a wide interval at
    # small n. The writeup has to quote both, so this returns both.
    d_tpr = out["delta"]["TPR"]["point"]
    d_fpr = out["delta"]["FPR"]["point"]
    tpr_ci = out["delta"]["TPR"]["ci95"]
    out["guard"] = {
        "TPR_delta": d_tpr,
        "TPR_ci95": tpr_ci,
        "TPR_held_at_point": d_tpr >= 0,
        # The one that decides the finding: did TPR fall by more than noise?
        "TPR_drop_significant": bool(tpr_ci and tpr_ci[1] < 0),
        "P(TPR fell)": out["delta"]["TPR"]["P(delta<=0)"],
        # How much of the FPR movement is matched by TPR movement in the same
        # direction. Near 1.0 means the judge simply refuses less of
        # everything — the string-match ALL control wearing a new hat. Near 0
        # means the change is specific to the falsely-refused class.
        "indiscriminate_ratio": (d_tpr / d_fpr) if d_fpr else None,
    }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", type=Path, required=True, help="baseline arm (v1)")
    ap.add_argument("--b", type=Path, required=True, help="revised arm (v2)")
    ap.add_argument("--label", default="")
    ap.add_argument("--reps", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=20260923)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    res = compare(args.a, args.b, args.label or f"{args.a.name} -> {args.b.name}",
                  args.reps, args.seed)
    print(json.dumps(res, indent=2))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
