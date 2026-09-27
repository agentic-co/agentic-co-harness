"""Literal-leak check (EXP-L.md v3): "no task-specific literals (names, dates,
amounts from EDIT tasks) in the diff."

The reviser is shown per-failed-task transcripts (user request, tool
calls/results) so it can see WHAT went wrong. It must not launder any of that
into the general document — a step that says "for Alex Green's booking, do
X" is a step that memorized one task instead of fixing the procedure. This
module extracts the literal, task-specific tokens out of a set of task
records (the same shape `scoring.score_file` returns per task: `user_prompt`
+ `tool_calls`) and checks whether any of them re-appear in the DIFF text
(the new/changed text only — the unchanged document is not scanned, since it
was already clean).

Deliberately conservative in what counts as a "literal": quoted strings,
dates, money amounts, emails, alphanumeric IDs, and multi-word Capitalized
sequences (names). Generic domain vocabulary ("single", "double", "booking")
is lowercase in the source tasks and will not match a Capitalized-sequence
rule, so a reviser is free to use ordinary domain language in a diff.
"""

from __future__ import annotations

import re
from typing import Iterable

_QUOTED_RE = re.compile(r'"([^"\n]{3,80})"|\'([^\'\n]{3,80})\'')
_PROPER_NOUN_RE = re.compile(r"\b[A-Z][a-zA-Z]{2,}(?:\s+[A-Z][a-zA-Z]{2,}){0,3}\b")
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b")
_AMOUNT_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d{1,2})?|\b\d+\.\d{2}\b")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_ID_RE = re.compile(r"\b[A-Z]{1,4}\d{2,}\b|\b\d{5,}\b")

# First-word stoplist for the Capitalized-sequence rule: sentence-starters and
# ASOP jargon that would otherwise false-positive as a "name" on every task
# whose user_prompt happens to start a sentence with one of these.
_STOPWORDS = {
    "the", "this", "that", "and", "for", "with", "from", "please", "hello",
    "hi", "i", "you", "your", "user", "tool", "call", "note", "step", "gate",
    "procedure", "routing", "asop", "would", "could", "can", "using", "of",
    "a", "an", "on", "in", "to", "is", "are", "yes", "no", "ok", "okay",
}


def extract_literals(text: str) -> set[str]:
    literals: set[str] = set()
    for m in _QUOTED_RE.finditer(text):
        val = (m.group(1) or m.group(2) or "").strip()
        if len(val) >= 3:
            literals.add(val)
    for m in _PROPER_NOUN_RE.finditer(text):
        val = m.group(0).strip()
        first = val.split()[0].lower()
        if first in _STOPWORDS:
            continue
        if len(val) >= 4:
            literals.add(val)
    for pat in (_DATE_RE, _AMOUNT_RE, _EMAIL_RE, _ID_RE):
        for m in pat.finditer(text):
            literals.add(m.group(0))
    return literals


def _values_only(obj) -> list[str]:
    """Recursively pull VALUES out of a JSON-shaped structure, never KEYS.

    Tool call arguments and results are dicts like `{"guest_name": "Alex
    Green", "check_in_date": "2024-12-04"}` — the field names are schema, not
    task-specific data, and a document is free to use them (a step legitimately
    says "read the `guest_name` argument"). `json.dumps`-ing the whole dict and
    scanning the blob for quoted substrings caught the KEYS too (measured
    live: "guest_name", "check_in_date" flagged as "leaks" that were really
    just the reviser naming a field). Only the values are task-specific.
    """
    out: list[str] = []
    if isinstance(obj, dict):
        for v in obj.values():
            out.extend(_values_only(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(_values_only(v))
    elif obj is not None:
        out.append(str(obj))
    return out


def _task_blob(record: dict) -> str:
    parts = [str(record.get("user_prompt", ""))]
    for tc in record.get("tool_calls", []):
        parts.extend(_values_only(tc.get("arguments", {})))
        parts.extend(_values_only(tc.get("content", "")))
    return "\n".join(parts)


def literals_from_records(records: Iterable[dict]) -> dict[str, set[str]]:
    """`{literal: {task labels it came from}}`, for a reporting trail."""
    out: dict[str, set[str]] = {}
    for rec in records:
        label = f"{rec.get('user_goal', '?')}#{rec.get('position', '?')}"
        for lit in extract_literals(_task_blob(rec)):
            out.setdefault(lit, set()).add(label)
    return out


def leak_check(diff_text: str, task_records: Iterable[dict]) -> list[str]:
    """Literal hits of any EDIT-task literal inside `diff_text`. Empty = clean."""
    hits: list[str] = []
    for literal, labels in literals_from_records(task_records).items():
        if literal and literal in diff_text:
            hits.append(f"literal {literal!r} from task(s) {sorted(labels)} appears in the diff")
    return hits
