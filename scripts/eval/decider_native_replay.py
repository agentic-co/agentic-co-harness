#!/usr/bin/env python3
"""Judge the N39 400-cell `hotel` sample with decider's NATIVE prompt shape.

    python3 scripts/eval/decider_native_replay.py \\
        --decisions /tmp/sb/hotel_native.jsonl \\
        --url http://127.0.0.1:4250/v1/systemone --label decider-0.8b \\
        --out evals/sopbench-hotel-asop/decider-0.8b-native

WHY THIS EXISTS
----------------
`sopbench_judge.py --jev --jev-url` (the N39 pass) replays TypeSafe Jev's own
prose prompt: a compound AND/OR prerequisite tree rendered as procedural rules
("`x` must have been called first ... AND of: ...") over a string-rendered,
300-char-truncated evidence transcript. Both shapes are named by decider's own
model card as its weak points -- "rules written into the question ... are not
followed reliably at this size" and "states can be strings, objects, or
arrays" (i.e. JSON is native, prose is not required). This script asks the
IDENTICAL 400 decisions (see QUEUED-EXPERIMENTS.md "decider replay v2" for the
id-reproduction check) a plain Noul question over a JSON state built from
`sopbench_extract.py`'s additive `raw_tool_calls`/`current_call` fields --
untruncated real tool call arguments and results, no embedded rule tree.

`verdict.choice == "true"` (via Noul) means "the tool results establish the
condition holds" -- the wire-level equivalent of Jev's `"held"` -- so scoring
maps directly onto `sopbench_judge.score()`.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
from sopbench_judge import score, stratified  # noqa: E402


def _free_mem_pct() -> float | None:
    """macOS `memory_pressure -Q`'s "System-wide memory free percentage: NN%" line, as a
    float, or None if the tool isn't available (e.g. not on macOS) -- --mem-floor-pct then
    has nothing to check against and the caller should treat that as "can't verify"."""
    try:
        out = subprocess.run(["memory_pressure", "-Q"], capture_output=True, text=True,
                              timeout=10).stdout
        m = re.search(r"free percentage:\s*(\d+)%", out)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def build_state(row: dict) -> dict:
    ev = row["evidence"]
    return {"tool_results": ev.get("raw_tool_calls", []), "params": ev.get("current_call", {})}


def build_question(row: dict) -> dict:
    tool = row["evidence"].get("current_call", {}).get("tool_name", row.get("tool_name", "this action"))
    return {
        "type": "noul",
        "instructions": (
            f"Is this condition satisfied: every prerequisite action required before "
            f"calling `{tool}` (with the given parameters) has already been completed "
            f"successfully, as shown in the tool results?"
        ),
        "criteria": {
            "true": "the tool results establish that every required prerequisite action "
                    "was already completed successfully",
            "false": "the tool results do not establish this, or show that a prerequisite "
                     "action was not completed or did not succeed",
        },
    }


def ask(url: str, row: dict, timeout: int = 60, retries: int = 3) -> dict:
    payload = {"state": build_state(row), "questions": {"verdict": build_question(row)}}
    raw = ""
    for attempt in range(retries):
        t0 = time.time()
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload, default=str).encode(),
                headers={"Content-Type": "application/json"})
            resp = json.load(urllib.request.urlopen(req, timeout=timeout))
            ans = resp["answers"]["verdict"]
            # Noul's wire answer is {"type": "noul", "noul": P(true)} -- no "choice" key
            # (systemone.py assemble()/format_answer(); confirmed against the live server,
            # unlike Choice's {"choice": ..., "confidence": ...}). P(true) >= 0.5 is "held".
            p_true = float(ans["noul"])
            passed = p_true >= 0.5
            conf = max(p_true, 1 - p_true)
            return {"id": row["_id"], "truth": row["truth"], "passed": passed,
                    "confidence": conf, "raw": json.dumps(ans)[:200],
                    "latency_s": round(time.time() - t0, 4)}
        except Exception as exc:
            raw = f"ERROR {type(exc).__name__}: {exc}"[:200]
            if attempt < retries - 1:
                time.sleep(1)
                continue
    return {"id": row["_id"], "truth": row["truth"], "passed": None,
            "confidence": 0.0, "raw": raw, "latency_s": 0.0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--decisions", type=Path, required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--per-class", type=int, default=200)
    ap.add_argument("--seed", type=int, default=20260923)
    ap.add_argument("--workers", type=int, default=1,
                    help="sequential (1) by default so latency_s reflects true per-call cost; "
                         "raise only for throughput, not for the latency table")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--mem-floor-pct", type=float, default=0,
                    help="abort cleanly (write whatever was scored so far, tagged partial) if "
                         "`memory_pressure -Q`'s free%% drops below this, checked every "
                         "--mem-check-every decisions. 0 disables the check (default: off, so "
                         "small local runs pay no cost for it).")
    ap.add_argument("--mem-check-every", type=int, default=10)
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.decisions) if l.strip()]
    sample = stratified(rows, args.per_class, args.seed)
    print(f"decisions available: {len(rows)}   sampled: {len(sample)} "
          f"({sum(1 for r in sample if r['truth']=='not_held')} not_held / "
          f"{sum(1 for r in sample if r['truth']=='held')} held)")

    out = []
    t0 = time.time()
    aborted_low_memory = False
    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            out = list(pool.map(lambda r: ask(args.url, r), sample))
    else:
        for i, r in enumerate(sample, 1):
            out.append(ask(args.url, r))
            if i % 50 == 0:
                rate = (time.time() - t0) / i
                print(f"    {i}/{len(sample)}  ({rate:.3f}s/call)", flush=True)
            if args.mem_floor_pct and i % args.mem_check_every == 0:
                free_pct = _free_mem_pct()
                if free_pct is not None and free_pct < args.mem_floor_pct:
                    print(f"[ABORT] memory_pressure free%={free_pct:.1f} < floor "
                          f"{args.mem_floor_pct}% after {i}/{len(sample)} decisions -- "
                          f"stopping cleanly, writing partial results", flush=True)
                    aborted_low_memory = True
                    break

    s = score(out, args.label)
    confs = [r["confidence"] for r in out if r["passed"] is not None]
    s["avg_confidence"] = sum(confs) / len(confs) if confs else None
    lat = sorted(r["latency_s"] for r in out if r["latency_s"])
    if lat:
        s["latency_median_s"] = lat[len(lat) // 2]
        s["latency_p95_s"] = lat[int(len(lat) * 0.95)]
    s["aborted_low_memory"] = aborted_low_memory
    s["n_sampled"] = len(sample)
    print(json.dumps(s, indent=2))
    args.out.mkdir(parents=True, exist_ok=True)
    tag = args.label.replace("/", "_").replace(".", "-")
    tag = tag + "_PARTIAL" if aborted_low_memory else tag
    (args.out / f"sopbench_native_{tag}.json").write_text(
        json.dumps({"summary": s, "decisions": out}, indent=2))
    return 1 if aborted_low_memory else 0


if __name__ == "__main__":
    raise SystemExit(main())
