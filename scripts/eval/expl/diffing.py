"""Targeted edits: one tagged search/replace diff per failure type (EXP-L.md
v3, max 3 per round), "applied in sequence and checked together" — every diff
in a round is applied before the combined self-check runs, so a round either
lands as a whole or is rejected as a whole (never a partial candidate).

Search/replace rather than unified-diff hunks: it is unambiguous to apply
(`str.replace`, one occurrence), unambiguous to fail loudly on (search text
absent, or matching more than once), and the reviser is asked to copy the
`search` text VERBATIM out of the document it was shown — so a hallucinated
diff is a diff that fails to find its own search text, not a diff that
silently patches the wrong place.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Diff:
    failure_type: str
    search: str
    replace: str


@dataclass(frozen=True)
class ApplyResult:
    document: str
    applied: tuple[str, ...]  # failure_types successfully applied, in order
    errors: tuple[str, ...]  # one entry per diff that could not be applied


def apply_diffs(document: str, diffs: list[Diff]) -> ApplyResult:
    doc = document
    applied: list[str] = []
    errors: list[str] = []
    for d in diffs:
        if not d.search:
            errors.append(f"{d.failure_type}: empty search text")
            continue
        count = doc.count(d.search)
        if count == 0:
            errors.append(
                f"{d.failure_type}: search text not found in document (hallucinated diff)"
            )
            continue
        if count > 1:
            errors.append(
                f"{d.failure_type}: search text matches {count} locations — "
                "refusing an ambiguous edit"
            )
            continue
        if d.search == d.replace:
            errors.append(f"{d.failure_type}: search and replace are identical — not an edit")
            continue
        doc = doc.replace(d.search, d.replace, 1)
        applied.append(d.failure_type)
    return ApplyResult(document=doc, applied=tuple(applied), errors=tuple(errors))


def diff_added_text(before: str, after: str) -> str:
    """The text worth running the literal-leak check over: lines that changed.

    A line-level diff (not a char diff) is enough for the leak check's
    purpose — it only needs to see the NEW content, not a minimal edit
    script — and is far simpler to reason about than character-level LCS.
    """
    import difflib

    before_lines = before.splitlines()
    after_lines = after.splitlines()
    added = []
    sm = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    for tag, _, _, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "insert"):
            added.extend(after_lines[j1:j2])
    return "\n".join(added)
