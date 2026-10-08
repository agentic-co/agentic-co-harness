#!/usr/bin/env python3
"""Shadow judge CLI. `scan` logs what the judge would do for parked gates;
`history` grades the judge against past human verdicts. Read-only on stores."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agentco_harness import judge_verifier as jv  # noqa: E402


def load_beads(paths: list[str]) -> list[dict]:
    """Read-only. Within one file, a repeated id keeps the latest `updated_at`
    (the live store's `get()` returns the FIRST; for judging we want the newest)."""
    out = []
    for p in paths:
        latest: dict[str, dict] = {}
        with open(p) as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    b = json.loads(line)
                except ValueError:
                    continue
                prev = latest.get(b.get("id"))
                if prev is None or (b.get("updated_at") or "") >= (prev.get("updated_at") or ""):
                    latest[b.get("id")] = b
        for b in latest.values():
            b["_source"] = p
            out.append(b)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("scan", "history"):
        sp = sub.add_parser(name)
        sp.add_argument("--tasks", nargs="+", required=True)
        sp.add_argument("--url", default=os.environ.get("CLEF_URL", jv.DEFAULT_URL))
        sp.add_argument("--threshold", type=float, default=jv.DEFAULT_THRESHOLD)
        sp.add_argument("--timeout", type=int, default=120)
    sub.choices["scan"].add_argument("--log", required=True)
    sub.choices["scan"].add_argument("--judged-only", action="store_true")
    sub.choices["history"].add_argument("--out", required=True)
    a = ap.parse_args(argv)
    beads = load_beads(a.tasks)
    if a.cmd == "scan":
        rows = jv.shadow_scan(beads, a.url, a.threshold, a.log, include_human=not a.judged_only,
                              timeout=a.timeout)
        print(json.dumps({"rows": len(rows), "log": a.log,
                          "would_auto_approve": sum(r["policy"] == "would_auto_approve" for r in rows)}))
    else:
        out = jv.history_eval(beads, a.url, a.threshold, timeout=a.timeout)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(out, indent=2))
        print(json.dumps(out["summary"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
