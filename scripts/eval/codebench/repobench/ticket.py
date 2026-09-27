"""Mechanically derive a symptom-only ticket from a failing test's own
pytest output — never the test's source, never the file or function name
under test. This is the module that makes the "ticket, not a test file" rule
real rather than aspirational: every value used in the returned ticket text
is pulled out of a regex match against pytest's OWN failure-report text, not
copied from source, and a specific redaction pass removes the mutated
function's name (and a few generic leak shapes — object addresses, bound-
method reprs) from whatever the regex captured.

Three tiers, tried in order, each fully mechanical:
  1. "assert-value"  — pytest's own `E   assert X == Y` line, or unittest's
                        `E   AssertionError: X != Y` line. Actual/expected
                        values only.
  2. "exception"     — no clean assert diff (the mutation raised instead of
                        returning wrong data); the exception type + message,
                        redacted.
  3. "fallback"      — neither pattern matched (a multi-line diff, a
                        hypothesis-style report, etc.). A generic ticket that
                        says a regression exists without a value pair. Kept
                        mutants that land here are the honest cost of NOT
                        hand-writing tickets — tracked, not hidden.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_MAX_VALUE_LEN = 200

_ASSERT_LINE_RE = re.compile(r"^E\s+assert\s+(.+?)\s+(==|!=)\s+(.+)$")
_UNITTEST_LINE_RE = re.compile(r"^E\s+AssertionError:\s+(.+?)\s+(!=|==)\s+(.+)$")
_EXC_LINE_RE = re.compile(r"^E\s+([A-Za-z_][\w.]*(?:Error|Exception|Warning)):\s*(.*)$")
_PARAM_SUFFIX_RE = re.compile(r"\[(.+)\]$")

_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")
_FUNCOBJ_RE = re.compile(r"<function \w+ at 0x[0-9a-fA-F]+>")
_BOUNDMETH_RE = re.compile(r"<bound method [\w.]+ of .*?>")


@dataclass(frozen=True)
class Ticket:
    text: str
    tier: str  # "assert-value" | "exception" | "fallback"


# Noise unittest/pytest prepend onto the ACTUAL side of a failure message —
# never information, always commentary about the diff that follows. Stripped
# rather than left in, since it reads as if it were part of the value.
_KNOWN_PREFIXES = (
    "assert ", "Lists differ: ", "Tuples differ: ", "Dicts differ: ",
    "Sets differ: ", "Items in the first set but not the second:",
)


def _strip_known_prefixes(text: str) -> str:
    for prefix in _KNOWN_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip()
    return text


def _redact(text: str, forbidden_names: set[str]) -> str:
    text = _FUNCOBJ_RE.sub("<function>", text)
    text = _BOUNDMETH_RE.sub("<method>", text)
    text = _ADDR_RE.sub("0x...", text)
    for name in forbidden_names:
        if name:
            text = re.sub(rf"\b{re.escape(name)}\b", "the function", text)
    return text


def _clip(text: str) -> str:
    return text if len(text) <= _MAX_VALUE_LEN else text[:_MAX_VALUE_LEN] + "...(truncated)"


def _param_note(node_id: str) -> str:
    m = _PARAM_SUFFIX_RE.search(node_id)
    if not m:
        return ""
    return f" It concerns input(s) described (by the test framework) as `{m.group(1)}`."


def derive_ticket(single_test_stdout: str, node_id: str, forbidden_names: set[str]) -> Ticket:
    """`single_test_stdout` must be `pytest <node_id> -q --tb=long`'s stdout
    for THAT ONE test — parsing the combined output of a whole-suite run
    would risk picking up a different test's traceback."""
    lines = single_test_stdout.splitlines()
    input_note = _param_note(node_id)

    for line in lines:
        assert_m = _ASSERT_LINE_RE.match(line)
        unittest_m = None if assert_m else _UNITTEST_LINE_RE.match(line)
        m = assert_m or unittest_m
        if m:
            actual, op, expected = m.group(1), m.group(2), m.group(3)
            actual = _clip(_redact(_strip_known_prefixes(actual), forbidden_names))
            expected = _clip(_redact(expected, forbidden_names))
            if assert_m:
                # pytest's assertion rewriting shows the SOURCE's literal
                # comparison operator (`assert X == Y` really did compare
                # with ==) — the shown op IS the intended relation.
                relation = "should NOT equal" if op == "!=" else "should equal"
            else:
                # unittest's assertEqual/assertNotEqual use a FIXED message
                # template regardless of source: assertEqual failing always
                # prints "!=" (verified empirically — it means "these came
                # out unequal, but should be equal"); assertNotEqual failing
                # always prints "==" ("these came out equal, but shouldn't
                # be"). The shown op is the CURRENT relation, so the intended
                # one is its opposite.
                relation = "should equal" if op == "!=" else "should NOT equal"
            return Ticket(
                text=(
                    "A behavior check in this project's own test suite is failing."
                    f"{input_note} The result currently produced is `{actual}`, but the "
                    f"check says it {relation} `{expected}`."
                ),
                tier="assert-value",
            )

    for line in reversed(lines):
        m = _EXC_LINE_RE.match(line)
        if m:
            exc_type, msg = m.group(1), _clip(_redact(m.group(2), forbidden_names))
            return Ticket(
                text=(
                    "A behavior check in this project's own test suite is failing."
                    f"{input_note} Instead of completing normally, it raises `{exc_type}`"
                    + (f" with message: \"{msg}\"." if msg else ".")
                ),
                tier="exception",
            )

    return Ticket(
        text=(
            "A test in this project's own suite is failing after a recent change, but no "
            "clean before/after value pair could be extracted automatically."
            f"{input_note} Something has regressed; find it via the test suite."
        ),
        tier="fallback",
    )
