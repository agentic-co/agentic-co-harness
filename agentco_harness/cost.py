"""Per-bead cost/latency ledger — the input to evidence-based model routing.

WHY THIS EXISTS
---------------
AgentCo picked models by judgment call, one config field at a time
(``triage.model``, ``feeds.ingest_model``, ``feeds.curate_model``, per-agent
``model:``). ``Plans/ModelRoutingEval.md`` was written in July 2026 to replace
that with measurement, and specified a synthetic harness: ~20 graded samples per
(task type x model) cell, run offline.

The cheaper path is the one the CLI was already handing us. Every headless run
returns ``total_cost_usd``, ``usage`` and ``modelUsage`` in its JSON envelope;
``executor.py`` parsed that envelope for ``stop_reason`` and discarded the rest.
Capturing it turns every production cycle into an eval sample, so the routing
table is built from real workloads rather than proxies for them.

The metric is deliberately COST PER COMPLETED BEAD, not cost per token. A model
at half the per-token price that burns twice the tokens is not cheaper, and only
a completion-denominated metric shows that.

FORMAT: append-only JSONL beside ``tasks.jsonl`` — git-friendly, quarantine-
preserving, same shape as ``runs.jsonl``. A malformed line is skipped on read,
never dropped from the file.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

LEDGER_NAME = "costs.jsonl"


def ledger_path(tasks_path: str | Path) -> Path:
    """The cost ledger lives beside the bead queue it describes."""
    return Path(tasks_path).parent / LEDGER_NAME


def record_run(
    tasks_path: str | Path,
    *,
    task_id: str,
    agent: str,
    exec_result: Any,
    company: Optional[str] = None,
    data_class: Optional[str] = None,
    task_type: Optional[str] = None,
    requested_model: Optional[str] = None,
    now: Optional[datetime] = None,
) -> None:
    """Append one execution's telemetry.

    Never raises: a telemetry failure must not fail real work. Failed runs are
    recorded too — an expensive failure is precisely what a cost review needs
    to see, and omitting them would bias every average optimistic.
    """
    try:
        entry = {
            "at": (now or datetime.now(timezone.utc)).isoformat(),
            "task_id": task_id,
            "agent": agent,
            "company": company,
            "data_class": data_class,
            "task_type": task_type,
            "requested_model": requested_model,
            "model_used": getattr(exec_result, "model_used", None),
            "success": bool(getattr(exec_result, "success", False)),
            "truncated": bool(getattr(exec_result, "truncated", False)),
            "cost_usd": getattr(exec_result, "cost_usd", None),
            "input_tokens": getattr(exec_result, "input_tokens", None),
            "output_tokens": getattr(exec_result, "output_tokens", None),
            "num_turns": getattr(exec_result, "num_turns", None),
            "duration_seconds": getattr(exec_result, "duration_seconds", None),
        }
        p = ledger_path(tasks_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception as e:  # noqa: BLE001 — telemetry is never load-bearing
        print(f"[cost] WARNING: could not record telemetry for {task_id}: {e}")


def read_ledger(tasks_path: str | Path) -> list[dict]:
    """Read every well-formed entry. Malformed lines are skipped, not dropped."""
    p = ledger_path(tasks_path)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def summarize(entries: Iterable[dict], group_by: str = "agent") -> list[dict]:
    """Aggregate to cost-per-completed-bead, the routing-decision metric.

    ``cost_per_completed`` divides total spend by SUCCESSFUL runs — so a cheap
    model that fails half its beads correctly shows up as expensive.
    """
    buckets: dict[Any, dict] = defaultdict(
        lambda: {"runs": 0, "completed": 0, "cost_usd": 0.0, "seconds": 0.0,
                 "in_tokens": 0, "out_tokens": 0, "priced_runs": 0}
    )
    for e in entries:
        b = buckets[e.get(group_by)]
        b["runs"] += 1
        if e.get("success"):
            b["completed"] += 1
        if isinstance(e.get("cost_usd"), (int, float)):
            b["cost_usd"] += float(e["cost_usd"])
            b["priced_runs"] += 1
        for src, dst in (("duration_seconds", "seconds"),
                         ("input_tokens", "in_tokens"),
                         ("output_tokens", "out_tokens")):
            if isinstance(e.get(src), (int, float)):
                b[dst] += e[src]

    rows = []
    for key, b in buckets.items():
        rows.append({
            group_by: key if key is not None else "(unset)",
            "runs": b["runs"],
            "completed": b["completed"],
            "success_rate": (b["completed"] / b["runs"]) if b["runs"] else 0.0,
            "cost_usd": round(b["cost_usd"], 4),
            # None (not 0.0) when nothing completed — an unknown cost must not
            # render as a free one.
            "cost_per_completed": round(b["cost_usd"] / b["completed"], 4) if b["completed"] else None,
            "avg_seconds": round(b["seconds"] / b["runs"], 1) if b["runs"] else 0.0,
            "input_tokens": b["in_tokens"],
            "output_tokens": b["out_tokens"],
            # Surfaced so a summary built from envelopes that carried no price
            # can't be mistaken for a summary showing zero spend.
            "priced_runs": b["priced_runs"],
        })
    rows.sort(key=lambda r: r["cost_usd"], reverse=True)
    return rows


def format_table(rows: list[dict], group_by: str) -> str:
    """Human-readable summary for `agentco cost`."""
    if not rows:
        return "No cost telemetry recorded yet.\n(Runs are recorded as beads execute; check back after a cycle.)"
    head = f"{group_by:<22} {'runs':>5} {'ok':>4} {'ok%':>5} {'$ total':>9} {'$/done':>8} {'avg s':>7} {'priced':>7}"
    lines = [head, "-" * len(head)]
    for r in rows:
        cpc = r["cost_per_completed"]
        lines.append(
            f"{str(r[group_by])[:22]:<22} {r['runs']:>5} {r['completed']:>4} "
            f"{r['success_rate'] * 100:>4.0f}% {r['cost_usd']:>9.4f} "
            f"{(f'{cpc:.4f}' if cpc is not None else '—'):>8} "
            f"{r['avg_seconds']:>7.1f} {r['priced_runs']:>7}"
        )
    total = sum(r["cost_usd"] for r in rows)
    done = sum(r["completed"] for r in rows)
    lines.append("-" * len(head))
    lines.append(f"{'TOTAL':<22} {sum(r['runs'] for r in rows):>5} {done:>4} "
                 f"{'':>5} {total:>9.4f} {(total / done if done else 0):>8.4f}")
    unpriced = sum(r["runs"] - r["priced_runs"] for r in rows)
    if unpriced:
        lines.append(f"\nNote: {unpriced} run(s) carried no price in their envelope "
                     f"(subscription-billed or older CLI) and contribute 0 to totals.")
    return "\n".join(lines)
