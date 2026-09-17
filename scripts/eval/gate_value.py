#!/usr/bin/env python3
"""Does a gate's refusal predict anything? Per-gate informativeness from real runs.

    python3 scripts/eval/gate_value.py <run-dir> [<run-dir> ...]

WHY THIS EXISTS (goal G3, finding N15). We know the refusal RATE: ~50% per gate,
which over ~11 gates per conversation compounds to a composite verdict that
refused **31 of 31 correct completions**. A gate that refuses everything is
indistinguishable from the always-refuse baseline this project criticised.

Rate alone does not say whether that is a problem, though. A gate could refuse
half of everything and still be useful, IF it refuses the bad half. So this asks
the next question:

    Given this gate refused, is the run more likely to have been wrong?

**LIFT** is the whole output: `P(refuse | run wrong) − P(refuse | run correct)`.

    lift > 0   the gate refuses failing runs more than passing ones — informative
    lift ≈ 0   the gate refuses both equally — it is NOISE, and its false
               refusals are pure cost
    lift < 0   the gate refuses CORRECT runs more often — actively misleading

`gate_probe.py` asks whether a gate *can* discriminate, against constructed
evidence, offline. This asks whether it *does*, against evidence a real run
produced. A gate can pass the first and fail the second — that is precisely the
interesting case, and it is the one the authoring guidance has no answer for.

⚠️ WHAT LIFT IS NOT. A gate verdict concerns one step's preconditions; the reward
concerns the whole conversation. A gate can correctly refuse a step in a run that
recovers and passes, and correctly pass steps in a run that fails elsewhere. So
lift is an **aggregate association, not a per-verdict correctness measure**, and a
single gate's lift over a handful of conversations is noise. Read the column with
its `n`, and do not quote a lift computed on fewer than ~10 conversations of
either class — the script marks those rather than hiding them.

What it is good for: finding gates whose refusals carry no signal at all, which
is an authoring defect the document can be changed to fix.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

#: Below this many conversations in either outcome class, a lift is not reportable.
#: Chosen so a gate seen in a handful of runs cannot produce a confident-looking
#: number — the failure mode this whole project keeps hitting.
MIN_PER_CLASS = 10


def load(run: Path) -> tuple[dict[int, bool], list[dict]]:
    """Reward per conversation index, and every applicable verdict."""
    results = json.loads((run / "results.json").read_text())
    reward = {
        i: (s.get("reward_info") or {}).get("reward", 0.0) >= 1.0
        for i, s in enumerate(results["simulations"])
    }
    verdicts = [
        v for v in (json.loads(l) for l in (run / "verdicts.jsonl").read_text().splitlines() if l.strip())
        if not v.get("not_applicable")
    ]
    return reward, verdicts


def per_gate(reward: dict[int, bool], verdicts: list[dict]) -> dict[str, dict]:
    """One row per gate (procedure · step), split by run outcome."""
    rows: dict[str, dict] = defaultdict(
        lambda: {"correct_n": 0, "correct_refused": 0, "wrong_n": 0, "wrong_refused": 0,
                 "kind": set()}
    )
    for v in verdicts:
        conv = v.get("conversation")
        if conv not in reward:
            continue
        key = f"{v.get('procedure','?')} · step {v.get('step','?')} {v.get('step_title','').strip()}"
        row = rows[key]
        row["kind"].add("det" if v.get("verifier") == "deterministic-check" else "judged")
        bucket = "correct" if reward[conv] else "wrong"
        row[f"{bucket}_n"] += 1
        if not v.get("passed"):
            row[f"{bucket}_refused"] += 1
    return rows


def report(run: Path) -> None:
    reward, verdicts = load(run)
    rows = per_gate(reward, verdicts)

    print(f"\n{run.name}   ({sum(reward.values())}/{len(reward)} runs correct, "
          f"{len(verdicts)} applicable verdicts)")
    print(f"  {'gate':<58} {'kind':<7} {'refuse|correct':>15} {'refuse|wrong':>13} {'lift':>7}")

    scored = []
    for key, r in sorted(rows.items()):
        kind = "+".join(sorted(r["kind"]))
        cn, wn = r["correct_n"], r["wrong_n"]
        pc = r["correct_refused"] / cn if cn else None
        pw = r["wrong_refused"] / wn if wn else None
        if pc is None or pw is None or cn < MIN_PER_CLASS or wn < MIN_PER_CLASS:
            note = f"n too small (correct {cn}, wrong {wn})"
            print(f"  {key[:58]:<58} {kind:<7} {note:>37}")
            continue
        lift = pw - pc
        scored.append((key, kind, pc, pw, lift))
        print(f"  {key[:58]:<58} {kind:<7} {pc:>14.2f} {pw:>13.2f} {lift:>+7.2f}")

    if not scored:
        print("\n  NOTHING REPORTABLE. Every gate fell below the "
              f"{MIN_PER_CLASS}-conversations-per-class floor, which usually means the "
              "run had too few failures to compare against — not that the gates are fine.")
        return

    noise = [s for s in scored if abs(s[4]) < 0.05]
    misleading = [s for s in scored if s[4] < -0.05]
    print(f"\n  reportable gates: {len(scored)}")
    print(f"  informative (lift > +0.05): {len([s for s in scored if s[4] > 0.05])}")
    print(f"  NOISE (|lift| < 0.05):      {len(noise)}")
    print(f"  MISLEADING (lift < -0.05):  {len(misleading)}")
    if noise:
        print("\n  Noise gates refuse correct and failing runs at the same rate. Their false"
              "\n  refusals are pure cost — they buy nothing. Candidates to cut or rewrite:")
        for key, _k, pc, pw, lift in noise:
            print(f"    {key[:70]}  ({pc:.2f} vs {pw:.2f})")
    print("\n  ⚠️ Lift is an aggregate association between a STEP's verdict and the RUN's"
          "\n     outcome, not a per-verdict correctness measure. Read it with its n.")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    for arg in argv:
        run = Path(arg).expanduser()
        if not (run / "verdicts.jsonl").exists():
            print(f"{run}: no verdicts.jsonl — not a gated run", file=sys.stderr)
            continue
        report(run)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
