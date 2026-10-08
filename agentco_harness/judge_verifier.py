"""Shadow-mode judge for ASOP judged gates: observe, never act.

A `human`/`judged` gate parks its bead at `awaiting_verify` until a person
answers. This module asks a local judge (Clef, SystemOne wire format) the same
question and RECORDS what it would have done. It is read-only by construction:
it takes bead dicts, appends to its own log, and imports nothing that can write
a bead store. tests/test_judge_verifier.py enforces that structurally.

There is deliberately no auto-reject outcome. The judge may only say "I would
have approved this"; every other case — `not_held`, low confidence, a dead
server — leaves the bead with the human.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

DEFAULT_URL = "http://127.0.0.1:8791/v1/systemone"
DEFAULT_THRESHOLD = 0.9
MAX_EVIDENCE_CHARS = 12_000
_OUTCOME_CAP = 4_000
_FIELD_CAP = 2_000
JUDGED_CLASSES = ("judged",)
HUMAN_CLASSES = ("human",)

_INSTRUCTIONS = (
    "A piece of delegated work has reported completion and must pass a verification gate "
    "before it is accepted. Given the task, the gate's check (and rubric, if any), and the "
    "executor's reported outcome, does the evidence establish that the gate's check is satisfied?"
)
_CRITERIA = {
    "held": "the evidence establishes the gate's check IS satisfied",
    "not_held": "the evidence does not establish the check is satisfied, or shows it is not",
}

Ask = Callable[[str, str, Optional[str], str, int], "tuple[Optional[bool], float, str]"]


# ── bead readers (dict-level; no store class, so nothing here can write one) ──


def gate_class(bead: dict) -> str | None:
    spec = (bead.get("metadata") or {}).get("verify") or {}
    return spec.get("kind") or spec.get("class")


def _check_text(spec: dict) -> str:
    if spec.get("checks"):
        return " -> ".join(str(s) for s in spec["checks"])
    return str(spec.get("check") or "")


def _outcome_text(bead: dict) -> str:
    """The executor's claim. `result` is a JSON string (claude CLI envelope or an
    asop `{"status","output"}`) or plain text; the gate's own output_tail is
    excluded because for human gates it carries the verdict being predicted."""
    raw = bead.get("result")
    if not raw:
        return ""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return raw
    if isinstance(raw, dict):
        for key in ("result", "output"):
            if raw.get(key):
                return str(raw[key])
        return json.dumps({k: v for k, v in raw.items() if k not in ("usage", "modelUsage")})[:_OUTCOME_CAP]
    return str(raw)


def _clip(text: str, cap: int, keep_tail: bool = False) -> str:
    if len(text) <= cap:
        return text
    if keep_tail:
        h = cap // 2
        return text[:h] + "\n[truncated middle]\n" + text[-h:]
    return text[:cap] + "\n[truncated]"


def build_evidence(bead: dict) -> str:
    meta = bead.get("metadata") or {}
    spec = meta.get("verify") or {}
    sop = meta.get("sop") if isinstance(meta.get("sop"), dict) else {}
    check = _clip(_check_text(spec), _FIELD_CAP)
    rubric = _clip(str(spec.get("rubric") or ""), _FIELD_CAP)
    outcome = _clip(_outcome_text(bead), _OUTCOME_CAP, keep_tail=True)

    head = [f"Title: {_clip(str(bead.get('title') or ''), 500)}"]
    for label, key in (("SOP purpose", "purpose"), ("SOP inputs", "inputs"),
                       ("Definition of done", "definition_of_done")):
        if sop.get(key):
            head.append(f"{label}: {_clip(str(sop[key]), 800)}")
    tail = [f"Gate check: {check}"]
    if rubric:
        tail.append(f"Gate rubric: {rubric}")
    tail.append(f"Executor's reported outcome:\n{outcome or '(none recorded)'}")

    desc = str(bead.get("description") or "")
    fixed = len("\n".join(head + tail)) + len("\nDescription: \n") + 40
    room = max(MAX_EVIDENCE_CHARS - fixed, 0)
    if len(desc) > room:
        half = room // 2
        desc = desc[:half] + "\n[truncated description middle]\n" + desc[len(desc) - half:] if half else ""
    parts = head + ([f"Description: {desc}"] if desc else []) + tail
    return "\n".join(parts)[:MAX_EVIDENCE_CHARS]


# ── judge ────────────────────────────────────────────────────────────────────


def ask_judge(evidence: str, check: str, rubric: Optional[str], url: str = DEFAULT_URL,
              timeout: int = 120, retries: int = 3) -> tuple[Optional[bool], float, str]:
    """One Clef call. `(held, confidence, raw)`; held is None on any failure."""
    state = evidence
    payload = {"model": "clef", "state": state,
               "questions": {"verdict": {"type": "choice", "instructions": _INSTRUCTIONS,
                                         "criteria": _CRITERIA}}}
    raw = ""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ans = json.load(resp)["answers"]["verdict"]
            return ans["choice"] == "held", float(ans.get("confidence", 0.0)), json.dumps(ans)[:200]
        except Exception as exc:  # judge failure must never reach callers
            raw = f"ERROR {type(exc).__name__}: {exc}"[:200]
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None, 0.0, raw


def policy(held: Optional[bool], confidence: float, threshold: float = DEFAULT_THRESHOLD) -> str:
    if held is True and confidence >= threshold:
        return "would_auto_approve"
    return "would_park_for_human"


def _judge(bead: dict, url: str, ask: Ask, timeout: int):
    spec = (bead.get("metadata") or {}).get("verify") or {}
    evidence = build_evidence(bead)
    t0 = time.monotonic()
    held, conf, raw = ask(evidence, _check_text(spec), spec.get("rubric"), url, timeout)
    return evidence, held, conf, raw, time.monotonic() - t0


def _verdict_str(held: Optional[bool]) -> Optional[str]:
    return None if held is None else ("held" if held else "not_held")


# ── shadow scan ──────────────────────────────────────────────────────────────


def shadow_scan(beads: Iterable[dict], url: str = DEFAULT_URL, threshold: float = DEFAULT_THRESHOLD,
                log_path: Path | str = "shadow.jsonl", ask: Ask = ask_judge, include_human: bool = True,
                timeout: int = 120) -> list[dict]:
    wanted = JUDGED_CLASSES + (HUMAN_CLASSES if include_human else ())
    rows = []
    for bead in beads:
        spec = (bead.get("metadata") or {}).get("verify") or {}
        if bead.get("status") != "awaiting_verify" or gate_class(bead) not in wanted:
            continue
        if not _check_text(spec):
            continue
        evidence, held, conf, raw, latency = _judge(bead, url, ask, timeout)
        rows.append({
            "bead_id": bead.get("id"), "source": bead.get("_source"), "gate_class": gate_class(bead),
            "check": _check_text(spec), "verdict": _verdict_str(held), "confidence": conf,
            "policy": policy(held, conf, threshold),
            "evidence_sha1": hashlib.sha1(evidence.encode()).hexdigest(),
            "latency_s": round(latency, 3), "ts": datetime.now(timezone.utc).isoformat(),
            "judge_url": url, "judge_model": "clef", "raw": raw,
        })
    log = Path(log_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return rows


# ── history eval ─────────────────────────────────────────────────────────────


def human_label(bead: dict) -> Optional[bool]:
    """True = a human approved, False = a human rejected, None = no human verdict.
    A `verify_failed` from a deterministic gate is a machine verdict, not a label."""
    meta = bead.get("metadata") or {}
    if meta.get("verify_approval"):
        return True
    if meta.get("verify_rejection"):
        return False
    if bead.get("status") == "verify_failed" and gate_class(bead) in HUMAN_CLASSES + JUDGED_CLASSES:
        return False
    return None


def history_eval(beads: Iterable[dict], url: str = DEFAULT_URL, threshold: float = DEFAULT_THRESHOLD,
                 ask: Ask = ask_judge, timeout: int = 120) -> dict:
    rows = []
    conf_m = {"tp": 0, "fn": 0, "fp": 0, "tn": 0, "unknown": 0}
    for bead in beads:
        label = human_label(bead)
        spec = (bead.get("metadata") or {}).get("verify") or {}
        if label is None or not _check_text(spec):
            continue
        evidence, held, conf, raw, latency = _judge(bead, url, ask, timeout)
        decision = policy(held, conf, threshold)
        if held is None:
            cell = "unknown"
        elif label:
            cell = "tp" if held else "fn"
        else:
            cell = "fp" if held else "tn"
        conf_m[cell] += 1
        rows.append({
            "bead_id": bead.get("id"), "source": bead.get("_source"), "gate_class": gate_class(bead),
            "label": "held" if label else "not_held", "verdict": _verdict_str(held),
            "confidence": conf, "policy": decision, "cell": cell,
            "dangerous": decision == "would_auto_approve" and not label,
            "evidence_sha1": hashlib.sha1(evidence.encode()).hexdigest(),
            "latency_s": round(latency, 3), "raw": raw,
        })
    n = len(rows)
    summary = {
        "n": n,
        "agreement": (conf_m["tp"] + conf_m["tn"]) / n if n else None,
        "confusion": conf_m,
        "threshold": threshold,
        "would_auto_approve": sum(r["policy"] == "would_auto_approve" for r in rows),
        "dangerous_errors": sum(r["dangerous"] for r in rows),
        "judge_url": url,
    }
    return {"summary": summary, "rows": rows}
