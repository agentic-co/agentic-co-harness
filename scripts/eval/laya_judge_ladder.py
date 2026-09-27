#!/usr/bin/env python3
"""Laya-as-judge, offline replay against ws_glm.jsonl's 68 labeled decisions.

    ~/Tools/laya-runtime/.venv/bin/python scripts/eval/laya_judge_ladder.py

Requires the `laya` package (see evals/tau2-airline-asop/LAYA-JUDGE.md for the
venv setup this repo does not own) and the eval archive checked out at
~/Code/agentco-harness-eval-archive/2026-09-15/ws_glm.jsonl.

Same methodology as evals/tau2-airline-asop/JUDGE-LADDER.md and
scripts/eval/gate_value.py: hold the evidence fixed, swap only the judge,
compute LIFT = P(refuse | truth=not_held) - P(refuse | truth=held), i.e. TPR - FPR.

Ground truth source: ws_glm.jsonl is the exact 68-decision labeled set behind
EVIDENCE.md's C1 confusion matrix (TP 25 / FP 1 / FN 33 / TN 9).

⚠️ EVIDENCE ORDERING IS LOAD-BEARING, and the first version of this script got it
wrong. `laya.common.build_sequence` truncates the state's TAIL (`st[:room]`), and
the base checkpoint leaves only 320 tokens for evidence (512 max_len - 192 head).
The original body put TOOL HISTORY *last* -- so the material the ground truth is
defined over is what fell off the end. `--order original` reproduces that, and it
is kept runnable only so the superseded numbers stay auditable; `evidence-first`
is the default and the correct one.

Raising `--max-len` above the checkpoint's native window is NOT a fix and makes
things worse -- these checkpoints are RLCD-trained at 512/1024 and degrade when run
long. The flag exists so that claim stays checkable rather than quoted.

Full writeup and result: evals/tau2-airline-asop/LAYA-JUDGE.md
"""
import argparse
import json
from pathlib import Path

from laya import Router

WS = Path.home() / "Code/agentco-harness-eval-archive/2026-09-15/ws_glm.jsonl"
OUT_DIR = Path(__file__).resolve().parent.parent.parent / "evals/tau2-airline-asop"


def build_body(ev: dict, order: str) -> str:
    """Render one decision's evidence. `order` decides what survives truncation."""
    precondition = f"STEP PRECONDITION: {ev.get('step_body', '')}"
    transcript = "TRANSCRIPT:\n" + "\n".join(ev.get("transcript", []))
    tool_history = "TOOL HISTORY:\n" + json.dumps(ev.get("tool_history", []))[:2000]
    if order == "original":
        return f"{precondition}\n\n{transcript}\n\n{tool_history}"
    return f"{tool_history}\n\n{precondition}\n\n{transcript}"


def load_labeled(path: Path) -> list[dict]:
    records = []
    with open(path) as f:
        for line in f:
            d = json.loads(line)
            if "_help" in d:
                continue
            if d.get("truth") in ("held", "not_held"):
                records.append(d)
    return records


def score(results: list[dict]) -> dict:
    truth_not_held = [r for r in results if r["truth"] == "not_held"]
    truth_held = [r for r in results if r["truth"] == "held"]

    def refuse_rate(rows):
        if not rows:
            return None
        refused = sum(1 for r in rows if r["laya_choice"] == "not_held")
        return refused / len(rows)

    tpr = refuse_rate(truth_not_held)  # catch rate on genuinely failed preconditions
    fpr = refuse_rate(truth_held)  # false-refusal rate on genuinely held preconditions
    lift = None if (tpr is None or fpr is None) else tpr - fpr

    tp = sum(1 for r in truth_not_held if r["laya_choice"] == "not_held")
    fn = sum(1 for r in truth_not_held if r["laya_choice"] == "held")
    fp = sum(1 for r in truth_held if r["laya_choice"] == "not_held")
    tn = sum(1 for r in truth_held if r["laya_choice"] == "held")

    return {
        "n": len(results),
        "n_truth_not_held": len(truth_not_held),
        "n_truth_held": len(truth_held),
        "TP": tp, "FP": fp, "FN": fn, "TN": tn,
        "recall_TPR": tpr,
        "FPR": fpr,
        "lift": lift,
        "precision": tp / (tp + fp) if (tp + fp) else None,
        "accuracy": (tp + tn) / len(results),
        "avg_confidence": sum(r["laya_confidence"] for r in results) / len(results),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--checkpoint", default="laya",
                    choices=["laya", "typed-decisions", "multilingual"])
    ap.add_argument("--order", default="evidence-first",
                    choices=["evidence-first", "original"],
                    help="'original' reproduces the superseded, truncation-biased body")
    ap.add_argument("--max-len", type=int, default=0,
                    help="override the checkpoint's native window; 0 keeps native (recommended)")
    args = ap.parse_args()

    records = load_labeled(WS)
    print(f"labeled records: {len(records)}")

    router = Router(preload=False)
    agent = router.load("english" if args.checkpoint == "laya" else args.checkpoint)
    native = agent.cfg.get("max_len", 512)
    if args.max_len:
        agent.cfg["max_len"] = args.max_len
    print(f"checkpoint={args.checkpoint} order={args.order} "
          f"max_len={agent.cfg.get('max_len')} (native {native})")

    question = {
        "type": "choice",
        "instructions": (
            "A procedure step has a stated precondition. Given the evidence below "
            "(the step's precondition text, the conversation transcript, and any tool "
            "calls/results), was the precondition actually satisfied?"
        ),
        "criteria": {
            "held": "the evidence establishes the precondition WAS satisfied",
            "not_held": "the evidence does not establish the precondition was satisfied, or shows it was not",
        },
    }

    kw = {} if args.checkpoint == "laya" else {"model": args.checkpoint}
    results = []
    for d in records:
        body = build_body(d["evidence"], args.order)
        res = router.predict({"body": body}, {"verdict": question}, **kw)
        ans = res["answers"]["verdict"]
        results.append({
            "id": d["_id"],
            "truth": d["truth"],
            "laya_choice": ans["choice"],
            "laya_confidence": ans["confidence"],
            "llm_passed": d["passed"],
        })

    summary = score(results)
    summary.update(checkpoint=args.checkpoint, order=args.order,
                   max_len=agent.cfg.get("max_len"))
    print(json.dumps(summary, indent=2))
    suffix = "" if args.checkpoint == "laya" else f"_{args.checkpoint}"
    if args.order != "evidence-first":
        suffix += f"_{args.order}"
    out = OUT_DIR / f"laya_judge_results{suffix}.json"
    out.write_text(json.dumps({"summary": summary, "decisions": results}, indent=2))
    print(f"\nfull results written to {out}")


if __name__ == "__main__":
    main()
