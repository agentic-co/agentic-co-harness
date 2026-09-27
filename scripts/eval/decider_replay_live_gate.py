#!/usr/bin/env python3
"""Replay the N36 LIVE Jev value-gate verdicts through a local `decider` server.

    python3 scripts/eval/decider_replay_live_gate.py \\
        --log ~/Code/agentco-harness-eval-archive/2026-09-24-session/scratchpad/confirm/gate/jev.verdicts.jsonl \\
        --url http://127.0.0.1:4250/v1/systemone --mode decider-0.8b \\
        --out /tmp/decider-0.8b.verdicts.jsonl

WHY THIS EXISTS
---------------
`sopbench_judge_gate.py`'s judge value gate logs one row per judged condition:
task_key/goal/constraint/polarity (so a row can be re-joined to SOPBench's own
truth by `grade_judge_gate.py`) plus `condition` (the verbalised precondition)
and `result` (the gating tool's own result, truncated to 200 chars) -- the
evidence the live gate actually showed Jev, MINUS the full tool_history and
user_known SOPBench also sends (the log records their SHA1, not their bytes,
so they cannot be reconstructed byte-for-byte). This script re-asks the
SAME question, over that reduced evidence, to a decider server -- an honest
replay of what the log actually preserved, not a claim of exact evidence
parity with the original Jev call. Stated once here, not re-derived: this is
narrower evidence than the 400-cell `sopbench_judge.py --jev` arm, which DOES
send full tool_history.

The output keeps every field `grade_judge_gate.py` needs (task_key, goal,
constraint, polarity) plus the new judge's own verdict/confidence/raw/latency,
so `grade_judge_gate.py --log <out>` grades it exactly as it grades a live
Jev or Laya log.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

_INSTRUCTIONS = (
    "A condition must hold before an action may be taken. Given the evidence "
    "(the gating tool's own result), does the evidence establish that the "
    "stated condition holds?"
)
_CRITERIA = {
    "held": "the evidence establishes the condition DOES hold",
    "not_held": "the evidence does not establish the condition holds, or shows it does not",
}


def evidence_body(result: str, condition: str) -> str:
    """Evidence first, then the question -- same ordering as `evidence_body`
    in `sopbench_judge_gate.py` and `body_of` in `sopbench_judge.judge_jev`."""
    return (f"TOOL RESULT:\n{result or '(no tool result recorded)'}\n\n"
            f"CONDITION THAT MUST HOLD: {condition}")


def ask(url: str, body: str, timeout: int = 60, retries: int = 4) -> tuple[bool | None, float, str, float]:
    payload = {"state": body, "questions": {"verdict": {
        "type": "choice", "instructions": _INSTRUCTIONS, "criteria": _CRITERIA}}}
    raw = ""
    for attempt in range(retries):
        t0 = time.time()
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            resp = json.load(urllib.request.urlopen(req, timeout=timeout))
            ans = resp["answers"]["verdict"]
            return ans["choice"] == "held", float(ans.get("confidence", 0.0)), \
                json.dumps(ans)[:200], round(time.time() - t0, 4)
        except Exception as exc:  # local server: no HTTP-error branching needed
            raw = f"ERROR {type(exc).__name__}: {exc}"[:200]
            if attempt < retries - 1:
                time.sleep(1)
                continue
            break
    return None, 0.0, raw, 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", type=Path, required=True, help="the LIVE jev.verdicts.jsonl to replay")
    ap.add_argument("--url", required=True, help="decider server's POST /v1/systemone endpoint")
    ap.add_argument("--mode", required=True, help="label written into every output row's 'mode'")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    rows = [json.loads(l) for l in args.log.read_text().splitlines() if l.strip()]
    print(f"replaying {len(rows)} rows from {args.log} against {args.url}")

    out = []
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        held, conf, raw, latency = ask(args.url, evidence_body(r.get("result", ""), r["condition"]))
        out.append({**r, "mode": args.mode, "verdict": held, "confidence": conf,
                     "raw": raw, "latency_s": latency,
                     "jev_verdict": r.get("verdict"), "jev_confidence": r.get("confidence")})
        if i % 100 == 0:
            rate = (time.time() - t0) / i
            print(f"  {i}/{len(rows)}  ({rate:.3f}s/call, ~{rate*(len(rows)-i)/60:.1f}min left)", flush=True)

    args.out.write_text("\n".join(json.dumps(r) for r in out) + "\n")
    errors = sum(1 for r in out if r["verdict"] is None)
    print(f"done: {len(out)} rows, {errors} errors, written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
