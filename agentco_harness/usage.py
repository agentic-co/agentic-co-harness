"""One metered boundary around every model-invoking subprocess.

WHY THIS EXISTS
---------------
AgentCo could not be metered. ``cost.py`` records a row only when a CALL SITE
remembers to call ``record_run``, so telemetry was a convention rather than a
property of execution: any new dispatch path — and there have been several —
silently added spend that no ledger could see. The failure mode is not "a
missing number", it is a fleet whose burn rate is unknowable, which is the one
property that makes scaling it unsafe. A 2M-token day and a session cap is what
that looks like from the outside.

The fix is deliberately small: ONE wrapper, ``meter()``, sits at the subprocess
launch. Every model-invoking path in ``executor.py`` goes through it, and each
execution emits exactly one row into an append-only JSONL ledger beside the
node's ``tasks.jsonl``. This is the 80%: a decorator around the launch, not a
socket-level meter.

ATTRIBUTION IS MANDATORY
------------------------
``meter()`` refuses to invoke a model it cannot attribute. Attribution is
established by the dispatcher — which bead, which lane, which node — and read
from a context variable so it cannot be forgotten as an argument on a new call
path: a launch outside an ``attributed()`` block raises ``MissingAttribution``
BEFORE the subprocess starts. Unattributed spend is not recorded as anonymous
spend; it is not permitted.

NULL, NEVER ZERO
----------------
A token count the CLI did not report is written as ``null``. Zero is a claim
("this run used no input tokens") and it is false; null is the truth ("this
route does not report it"). Every consumer here distinguishes the two, so a
route that reports nothing can never be mistaken for a free one.

FORMAT: append-only JSONL, one object per line, ``schema`` stamped for forward
compatibility — git-friendly and quarantine-preserving, same doctrine as
``tasks.jsonl``, ``runs.jsonl`` and ``costs.jsonl``.
"""

from __future__ import annotations

import json
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional, TypeVar

LEDGER_NAME = "usage.jsonl"

#: Stamped on every row. Bump only for a breaking shape change; readers here
#: tolerate rows without it (there are none today, but a hand-written row or a
#: future importer should not be dropped).
SCHEMA = "agentco_harness.usage/1"

T = TypeVar("T")


class MissingAttribution(RuntimeError):
    """A model was about to be invoked with no answer to "what is this for?".

    Raised BEFORE the subprocess launches. Fail loudly at the layer where the
    failure occurs: the layer that cannot attribute the spend is the layer that
    must not spend.
    """


# ---------------------------------------------------------------- attribution


@dataclass(frozen=True)
class Attribution:
    """Who this execution is for. Every field below ``tasks_path`` is optional
    context; the first four are the question "what is this for?" and there is no
    default for any of them.

    ``lane`` is the pipeline that dispatched the run (``cycle``, ``chat``,
    ``planner``, ``forge``, ``feeds``...). ``node`` is the AgentCo node the
    spend belongs to — derived from ``tasks_path`` when not given, because the
    store's location IS the node's identity.
    """

    bead_id: str
    lane: str
    tasks_path: str
    node: str = ""
    company: Optional[str] = None
    task_type: Optional[str] = None
    data_class: Optional[str] = None
    requested_model: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("bead_id", "lane", "tasks_path"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise MissingAttribution(
                    f"usage attribution is incomplete: {name!r} is required and was "
                    f"{value!r} — a model invocation that cannot say what it is for "
                    f"must not run"
                )
        if not self.node:
            object.__setattr__(self, "node", node_name(self.tasks_path))


def node_name(tasks_path: str | Path) -> str:
    """The node a store belongs to, from where the store lives.

    Company nodes keep their store at ``<repo>/.agentco/tasks.jsonl``, so the
    repo directory is the name that means something; the hub keeps it at the
    repo root. Falls back to the parent directory name for anything else.
    """
    parent = Path(tasks_path).expanduser().parent
    if parent.name == ".agentco":
        return parent.parent.name or "unknown"
    return parent.name or "unknown"


_CURRENT: ContextVar[Optional[Attribution]] = ContextVar(
    "agentco_usage_attribution", default=None
)


@contextmanager
def attributed(**kwargs: Any) -> Iterator[Attribution]:
    """Declare what the executions inside this block are for.

    The dispatcher knows the bead and the lane; the executor does not and must
    not guess. Nesting is supported and the innermost block wins — a sub-run
    dispatched inside another lane is attributed to the inner one.
    """
    attribution = Attribution(**kwargs)
    token = _CURRENT.set(attribution)
    try:
        yield attribution
    finally:
        _CURRENT.reset(token)


def current() -> Optional[Attribution]:
    """The attribution in force, or None outside an ``attributed()`` block."""
    return _CURRENT.get()


def require_attribution(explicit: Optional[Attribution] = None) -> Attribution:
    """The attribution to charge, or raise. Never returns a placeholder."""
    attribution = explicit if explicit is not None else _CURRENT.get()
    if attribution is None:
        raise MissingAttribution(
            "no usage attribution in scope — a model-invoking path must run "
            "inside `usage.attributed(bead_id=..., lane=..., tasks_path=...)` "
            "so its spend can be charged to something. Refusing to invoke."
        )
    if not isinstance(attribution, Attribution):
        raise MissingAttribution(
            f"usage attribution must be an Attribution, got {type(attribution).__name__}"
        )
    return attribution


# --------------------------------------------------------------------- ledger


def ledger_path(tasks_path: str | Path) -> Path:
    """The usage ledger lives beside the bead store whose work it describes."""
    return Path(tasks_path).expanduser().parent / LEDGER_NAME


def _nullable_number(value: Any) -> Optional[float | int]:
    """A number, or None. Booleans are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def record_usage(
    attribution: Attribution,
    *,
    executor: str,
    route: str,
    model_used: Optional[str] = None,
    duration_seconds: float = 0.0,
    exit_status: str = "unknown",
    exit_code: Optional[int] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    cache_read_tokens: Optional[int] = None,
    cache_creation_tokens: Optional[int] = None,
    num_turns: Optional[int] = None,
    cost_usd: Optional[float] = None,
    error: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Append exactly one usage row and return it.

    Never raises on a write failure: telemetry must not be able to fail real
    work. It DOES raise on a bad attribution — but that check has already run in
    ``meter()`` before the model was invoked, so by here it cannot fire.
    """
    row = {
        "schema": SCHEMA,
        "at": (now or datetime.now(timezone.utc)).isoformat(),
        "bead_id": attribution.bead_id,
        "lane": attribution.lane,
        "node": attribution.node,
        "company": attribution.company,
        "task_type": attribution.task_type,
        "data_class": attribution.data_class,
        "executor": executor,
        "route": route,
        "model_requested": attribution.requested_model,
        "model_used": model_used,
        "duration_seconds": round(float(duration_seconds), 3),
        "exit_status": exit_status,
        "exit_code": exit_code,
        # NULL, never 0 — see the module header.
        "input_tokens": _nullable_number(input_tokens),
        "output_tokens": _nullable_number(output_tokens),
        "cache_read_tokens": _nullable_number(cache_read_tokens),
        "cache_creation_tokens": _nullable_number(cache_creation_tokens),
        "total_tokens": None,
        "num_turns": _nullable_number(num_turns),
        "cost_usd": _nullable_number(cost_usd),
        "error": (error[:400] if isinstance(error, str) else None),
    }
    if row["input_tokens"] is not None or row["output_tokens"] is not None:
        row["total_tokens"] = (row["input_tokens"] or 0) + (row["output_tokens"] or 0)
    if attribution.extra:
        row["extra"] = attribution.extra

    try:
        path = ledger_path(attribution.tasks_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception as e:  # noqa: BLE001 — telemetry is never load-bearing
        print(f"[usage] WARNING: could not record usage for {attribution.bead_id}: {e}")
    return row


def read_ledger(tasks_path: str | Path) -> list[dict]:
    """Every well-formed row. A malformed line is skipped on read, never dropped
    from the file — quarantine, not deletion."""
    path = ledger_path(tasks_path)
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


# ---------------------------------------------------------------- the wrapper


def _classify_exit(result: Any) -> tuple[str, Optional[int], Optional[str]]:
    """(exit_status, exit_code, error) for an ExecResult-shaped object."""
    error = getattr(result, "error", None)
    exit_code = getattr(result, "exit_code", None)
    if getattr(result, "idle_timeout_hit", False):
        return "idle_timeout", exit_code, error
    if getattr(result, "truncated", False):
        return "truncated", exit_code, error
    if getattr(result, "success", False):
        return "ok", exit_code, None
    if exit_code is None and isinstance(error, str) and "timed out" in error:
        return "timeout", None, error
    return "failed", exit_code, error


def meter(
    run: Callable[[], T],
    *,
    executor: str,
    route: str,
    attribution: Optional[Attribution] = None,
    now: Optional[datetime] = None,
) -> T:
    """THE metered boundary. Run `run()`, emit exactly one usage row.

    Attribution is resolved and validated BEFORE `run()` is called, so a path
    that cannot say what it is for never reaches the subprocess. An exception
    out of `run()` is recorded as an ``error`` row and then re-raised unchanged
    — a crash that burned tokens is exactly the run a cost review needs to see,
    and swallowing it would make the meter load-bearing, which it must not be.
    """
    charged = require_attribution(attribution)
    started = _monotonic()
    try:
        result = run()
    except BaseException as e:  # noqa: BLE001 — recorded, then re-raised as-is
        record_usage(
            charged,
            executor=executor,
            route=route,
            duration_seconds=_monotonic() - started,
            exit_status="error",
            error=f"{type(e).__name__}: {e}",
            now=now,
        )
        raise

    duration = getattr(result, "duration_seconds", None)
    if not isinstance(duration, (int, float)):
        duration = _monotonic() - started
    exit_status, exit_code, error = _classify_exit(result)
    record_usage(
        charged,
        executor=executor,
        route=route,
        model_used=getattr(result, "model_used", None),
        duration_seconds=duration,
        exit_status=exit_status,
        exit_code=exit_code,
        input_tokens=getattr(result, "input_tokens", None),
        output_tokens=getattr(result, "output_tokens", None),
        cache_read_tokens=getattr(result, "cache_read_tokens", None),
        cache_creation_tokens=getattr(result, "cache_creation_tokens", None),
        num_turns=getattr(result, "num_turns", None),
        cost_usd=getattr(result, "cost_usd", None),
        error=error,
        now=now,
    )
    return result


def _monotonic() -> float:
    import time

    return time.monotonic()


# ------------------------------------------------------------------ reporting

GROUP_KEYS = ("day", "model", "bead", "node", "lane", "executor", "route")

_GROUP_FIELD = {
    "model": "model_used",
    "bead": "bead_id",
    "node": "node",
    "lane": "lane",
    "executor": "executor",
    "route": "route",
}


def _group_value(row: dict, group_by: str) -> str:
    if group_by == "day":
        at = row.get("at")
        return at[:10] if isinstance(at, str) and len(at) >= 10 else "(undated)"
    value = row.get(_GROUP_FIELD.get(group_by, group_by))
    return str(value) if value not in (None, "") else "(unset)"


def summarize(rows: Iterable[dict], group_by: str = "day") -> list[dict]:
    """Aggregate usage rows on one dimension.

    Counts of runs are always exact. Token and cost sums carry their own
    denominators (``priced_runs``, ``token_runs``) so a total assembled from
    routes that report nothing can never be read as a total of zero spend.
    """
    buckets: dict[str, dict] = defaultdict(
        lambda: {
            "runs": 0,
            "ok": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "seconds": 0.0,
            "priced_runs": 0,
            "token_runs": 0,
        }
    )
    for row in rows:
        b = buckets[_group_value(row, group_by)]
        b["runs"] += 1
        if row.get("exit_status") == "ok":
            b["ok"] += 1
        cost = _nullable_number(row.get("cost_usd"))
        if cost is not None:
            b["cost_usd"] += float(cost)
            b["priced_runs"] += 1
        seen_tokens = False
        for src, dst in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens")):
            value = _nullable_number(row.get(src))
            if value is not None:
                b[dst] += value
                seen_tokens = True
        if seen_tokens:
            b["token_runs"] += 1
        seconds = _nullable_number(row.get("duration_seconds"))
        if seconds is not None:
            b["seconds"] += float(seconds)

    out = []
    for key, b in buckets.items():
        out.append(
            {
                group_by: key,
                "runs": b["runs"],
                "ok": b["ok"],
                "success_rate": (b["ok"] / b["runs"]) if b["runs"] else 0.0,
                # None, not 0: nothing reported tokens here.
                "input_tokens": b["input_tokens"] if b["token_runs"] else None,
                "output_tokens": b["output_tokens"] if b["token_runs"] else None,
                "total_tokens": (
                    b["input_tokens"] + b["output_tokens"] if b["token_runs"] else None
                ),
                "cost_usd": round(b["cost_usd"], 4) if b["priced_runs"] else None,
                "avg_seconds": round(b["seconds"] / b["runs"], 1) if b["runs"] else 0.0,
                "priced_runs": b["priced_runs"],
                "token_runs": b["token_runs"],
            }
        )
    out.sort(key=lambda r: (r["cost_usd"] or 0.0, r["runs"]), reverse=True)
    return out


def totals(rows: Iterable[dict]) -> dict:
    """Fleet-wide totals, with the unreported denominators kept visible."""
    rows = list(rows)
    priced = [r for r in rows if _nullable_number(r.get("cost_usd")) is not None]
    tokened = [
        r
        for r in rows
        if _nullable_number(r.get("input_tokens")) is not None
        or _nullable_number(r.get("output_tokens")) is not None
    ]
    return {
        "runs": len(rows),
        "ok": sum(1 for r in rows if r.get("exit_status") == "ok"),
        "beads": len({r.get("bead_id") for r in rows if r.get("bead_id")}),
        "cost_usd": round(sum(float(r["cost_usd"]) for r in priced), 4) if priced else None,
        "priced_runs": len(priced),
        "input_tokens": sum(_nullable_number(r.get("input_tokens")) or 0 for r in tokened)
        if tokened
        else None,
        "output_tokens": sum(_nullable_number(r.get("output_tokens")) or 0 for r in tokened)
        if tokened
        else None,
        "token_runs": len(tokened),
        "unreported_cost_runs": len(rows) - len(priced),
        "unreported_token_runs": len(rows) - len(tokened),
        "first_at": min((r.get("at") for r in rows if r.get("at")), default=None),
        "last_at": max((r.get("at") for r in rows if r.get("at")), default=None),
    }


def within(rows: Iterable[dict], days: Optional[int], now: Optional[datetime] = None) -> list[dict]:
    """Rows stamped within the last `days`. `days=None` keeps everything.

    A row whose timestamp cannot be parsed is KEPT — dropping it would quietly
    shrink a total, and an unreadable timestamp is a defect in the row, not
    evidence that the run did not happen.
    """
    if not days:
        return list(rows)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    kept = []
    for row in rows:
        at = row.get("at")
        try:
            stamped = datetime.fromisoformat(str(at))
        except (TypeError, ValueError):
            kept.append(row)
            continue
        if stamped.tzinfo is None:
            stamped = stamped.replace(tzinfo=timezone.utc)
        if stamped >= cutoff:
            kept.append(row)
    return kept


def format_table(rows: list[dict], group_by: str) -> str:
    """Human-readable summary for `agentco usage`."""
    if not rows:
        return (
            "No usage telemetry recorded yet.\n"
            "(One row is written per metered model invocation — check back after a cycle.)"
        )

    def num(value, width: int, fmt: str) -> str:
        return f"{'—':>{width}}" if value is None else f"{value:>{width}{fmt}}"

    head = (
        f"{group_by:<24} {'runs':>5} {'ok':>4} {'in tok':>10} {'out tok':>10} "
        f"{'$ total':>9} {'avg s':>7} {'priced':>7}"
    )
    lines = [head, "-" * len(head)]
    for r in rows:
        lines.append(
            f"{str(r[group_by])[:24]:<24} {r['runs']:>5} {r['ok']:>4} "
            f"{num(r['input_tokens'], 10, ',')} {num(r['output_tokens'], 10, ',')} "
            f"{num(r['cost_usd'], 9, '.4f')} {r['avg_seconds']:>7.1f} {r['priced_runs']:>7}"
        )
    lines.append("-" * len(head))
    total_runs = sum(r["runs"] for r in rows)
    total_ok = sum(r["ok"] for r in rows)
    priced = sum(r["priced_runs"] for r in rows)
    cost = sum(r["cost_usd"] or 0.0 for r in rows)
    in_tok = sum(r["input_tokens"] or 0 for r in rows)
    out_tok = sum(r["output_tokens"] or 0 for r in rows)
    lines.append(
        f"{'TOTAL':<24} {total_runs:>5} {total_ok:>4} "
        f"{in_tok:>10,} {out_tok:>10,} "
        f"{(cost if priced else 0.0):>9.4f} {'':>7} {priced:>7}"
    )
    unpriced = total_runs - priced
    if unpriced:
        lines.append(
            f"\nNote: {unpriced} of {total_runs} run(s) reported no price "
            f"(subscription-billed, or a route that does not report cost). They are "
            f"counted as runs and contribute nothing to the $ column — not as $0."
        )
    return "\n".join(lines)


# -------------------------------------------------------------- the gap check

#: Model-invoking paths that are NOT yet metered, and why. Named here rather
#: than left implicit so `doctor` can say what it does not know: an unmetered
#: path the operator has never heard of is worse than one on a list.
UNMETERED_PATHS: tuple[tuple[str, str], ...] = (
    (
        "agents.py implement-prompt subprocess",
        "legacy DSPy agent path, not reached by the cycle dispatcher",
    ),
    (
        "orchestrator DSPy triage LM",
        "in-process dspy.LM call, metered only by the socket meter v2 specifies",
    ),
    (
        "extension handlers (register_cycle_handler / register_completion_hook)",
        "one-shot calls made by registered extensions with no bead of their own — "
        "the extension owns attribution; v1's retro/transkriptor/emailfeed sat here",
    ),
)

def unmetered_beads(
    rows: Iterable[dict],
    executed_bead_ids: Iterable[str],
) -> list[str]:
    """Bead ids that ran on an agent route but produced no usage row.

    THE gap to detect. A path that dispatches a model without going through
    ``meter()`` is invisible by construction — it cannot be caught by looking at
    the ledger, only by comparing the ledger against work that is known to have
    executed. Sorted for a stable report.
    """
    metered = {r.get("bead_id") for r in rows if r.get("bead_id")}
    return sorted({bid for bid in executed_bead_ids if bid and bid not in metered})
