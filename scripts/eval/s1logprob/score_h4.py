#!/usr/bin/env python3
"""Score an s1_logprob_judge.py output file the same way sopbench_judge.py's
`score()` does (TP/FP/FN/TN, TPR, FPR, lift +/- 95% Wald CI), plus the extra
tables H4 asked for: a calibration table over confidence bins, the
confident-block-vs-confident-pass precision split (N38's asymmetry check),
and latency.

Reuses `score()` verbatim from scripts/eval/sopbench_judge.py rather than
reimplementing lift/CI arithmetic a second time.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

REPO = Path.home() / "Code" / "agentco-harness"
sys.path.insert(0, str(REPO / "scripts" / "eval"))
from sopbench_judge import score  # noqa: E402

BINS = [(0.0, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 0.95), (0.95, 0.99), (0.99, 1.0001)]


def calibration(rows: list[dict]) -> list[dict]:
    """Accuracy per confidence bin on the CHOSEN label (passed==truth=='held')."""
    out = []
    for lo, hi in BINS:
        bucket = [r for r in rows if lo <= r["confidence"] < hi]
        if not bucket:
            out.append({"bin": f"[{lo},{hi})", "n": 0, "acc": None})
            continue
        correct = sum(1 for r in bucket
                       if (r["passed"] and r["truth"] == "held")
                       or (not r["passed"] and r["truth"] == "not_held"))
        out.append({"bin": f"[{lo},{hi})", "n": len(bucket), "acc": correct / len(bucket)})
    return out


def confident_block_vs_pass(rows: list[dict], conf: float = 0.95) -> dict:
    """N38's asymmetry check: precision of confident BLOCKS (passed=False)
    vs confident PASSES (passed=True) at confidence >= conf."""
    conf_rows = [r for r in rows if r["confidence"] >= conf]
    blocks = [r for r in conf_rows if r["passed"] is False]
    passes = [r for r in conf_rows if r["passed"] is True]
    block_prec = (sum(1 for r in blocks if r["truth"] == "not_held") / len(blocks)
                  if blocks else None)
    pass_prec = (sum(1 for r in passes if r["truth"] == "held") / len(passes)
                 if passes else None)
    return {"conf_threshold": conf, "n_blocks": len(blocks), "n_passes": len(passes),
            "block_precision": block_prec, "pass_precision": pass_prec}


def main() -> int:
    path = Path(sys.argv[1])
    d = json.loads(path.read_text())
    rows = d["decisions"]
    s = score(rows, f"{d['model']} ({d['variant']}"
                     + (f", budget={d['budget']}" if d.get("budget") else "") + ")")
    lat = [r["latency_s"] for r in rows]
    out = {
        "summary": s,
        "avg_confidence": statistics.fmean(r["confidence"] for r in rows),
        "calibration": calibration(rows),
        "confident_block_vs_pass_95": confident_block_vs_pass(rows, 0.95),
        "latency_s": {"mean": statistics.fmean(lat), "median": statistics.median(lat),
                       "p95": sorted(lat)[int(0.95 * len(lat)) - 1], "max": max(lat)},
    }
    if any("n_reasoning_tokens" in r for r in rows):
        rt = [r["n_reasoning_tokens"] for r in rows]
        out["reasoning_tokens"] = {"mean": statistics.fmean(rt), "median": statistics.median(rt),
                                     "max": max(rt), "min": min(rt)}
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
