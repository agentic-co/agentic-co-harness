#!/usr/bin/env python3
"""Jev-as-judge, offline replay against ws_glm.jsonl's 68 labeled decisions.

    python3 scripts/eval/jev_judge_ladder.py

Same methodology as laya_judge_ladder.py and JUDGE-LADDER.md: hold the
evidence fixed, swap only the judge, compute LIFT = P(refuse | truth=not_held)
- P(refuse | truth=held), i.e. TPR - FPR (Youden's J).

Ground truth source: ws_glm.jsonl is the exact 68-decision labeled set behind
EVIDENCE.md's C1 confusion matrix (TP 25 / FP 1 / FN 33 / TN 9).

Requires TYPESAFE_API_KEY in env or ~/.claude/.env.
"""
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

WS = Path.home() / "Code/agentco-harness-eval-archive/2026-09-15/ws_glm.jsonl"
JEV_URL = "https://api.typesafe.ai/v1/systemone"
ENV_FILE = pathlib.Path.home() / ".claude" / ".env"


def jev_key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith("TYPESAFE_API_KEY="):
            val = line.split("=", 1)[1].strip().strip('"').strip("'")
            if val:
                return val
    sys.exit("TYPESAFE_API_KEY not found")


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
        refused = sum(1 for r in rows if r["jev_choice"] == "not_held")
        return refused / len(rows)

    tpr = refuse_rate(truth_not_held)
    fpr = refuse_rate(truth_held)
    lift = None if (tpr is None or fpr is None) else tpr - fpr

    tp = sum(1 for r in truth_not_held if r["jev_choice"] == "not_held")
    fn = sum(1 for r in truth_not_held if r["jev_choice"] == "held")
    fp = sum(1 for r in truth_held if r["jev_choice"] == "not_held")
    tn = sum(1 for r in truth_held if r["jev_choice"] == "held")

    return {
        "n": len(results), "n_truth_not_held": len(truth_not_held),
        "n_truth_held": len(truth_held),
        "TP": tp, "FP": fp, "FN": fn, "TN": tn,
        "recall_TPR": tpr, "FPR": fpr, "lift": lift,
        "precision": tp / (tp + fp) if (tp + fp) else None,
        "accuracy": (tp + tn) / len(results),
        "avg_confidence": sum(r["jev_confidence"] for r in results) / len(results),
    }


def main() -> None:
    key = jev_key()
    records = load_labeled(WS)
    print(f"labeled records: {len(records)}")

    question = {
        "type": "choice",
        "instructions": (
            "A procedure step has a stated precondition. Given the evidence below "
            "(tool history, the step's precondition text, and the conversation "
            "transcript), was the precondition actually satisfied?"
        ),
        "criteria": {
            "held": "the evidence establishes the precondition WAS satisfied",
            "not_held": "the evidence does not establish the precondition was satisfied, or shows it was not",
        },
    }

    results = []
    for i, d in enumerate(records, 1):
        ev = d["evidence"]
        transcript = "\n".join(ev.get("transcript", []))
        tool_history = json.dumps(ev.get("tool_history", []))[:2000]
        # evidence-first, matching Laya's corrected ordering
        body = (
            f"TOOL HISTORY:\n{tool_history}\n\n"
            f"STEP PRECONDITION: {ev.get('step_body', '')}\n\n"
            f"TRANSCRIPT:\n{transcript}"
        )
        payload = {"model": "jev-latest", "state": body, "questions": {"verdict": question}}
        for attempt in range(4):
            try:
                req = urllib.request.Request(
                    JEV_URL, data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json",
                             "Authorization": f"Bearer {key}"})
                resp = json.load(urllib.request.urlopen(req, timeout=60))
                ans = resp["answers"]["verdict"]
                results.append({
                    "id": d["_id"], "truth": d["truth"],
                    "jev_choice": ans["choice"],
                    "jev_confidence": ans.get("confidence", 0.0),
                    "llm_passed": d["passed"],
                })
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 529) and attempt < 3:
                    time.sleep(2 ** attempt * 2)
                    continue
                print(f"error on {d['_id']}: HTTP {exc.code} {exc.read().decode()[:160]}")
                break
        if i % 20 == 0:
            print(f"  {i}/{len(records)}")

    print(json.dumps(score(results), indent=2))
    out = Path(__file__).resolve().parent.parent.parent / "evals/tau2-airline-asop/jev_judge_results.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"\nfull results written to {out}")


if __name__ == "__main__":
    main()
