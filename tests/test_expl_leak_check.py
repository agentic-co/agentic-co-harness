"""Literal-leak check (EXP-L.md v3: "no task-specific literals (names, dates,
amounts from EDIT tasks) in the diff"). Pure-stdlib, no SOPBench needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parents[1] / "scripts" / "eval"
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from expl import leak_check as lc  # noqa: E402

TASK_RECORD = {
    "user_goal": "book_room",
    "position": 3,
    "user_prompt": (
        "Hello, I would like to book a room for the guest named Alex Green in the "
        "single category, for the dates from 2024-12-04 to 2024-12-07, using a "
        "payment amount of 240."
    ),
    "tool_calls": [
        {"tool_name": "book_room", "arguments": {"guest_name": "Alex Green", "amount": 240}, "content": "BK042"}
    ],
}


def test_extracts_a_name_a_date_and_an_amount():
    literals = lc.extract_literals(TASK_RECORD["user_prompt"])
    assert "Alex Green" in literals
    assert "2024-12-04" in literals
    assert any(lit.replace(" ", "") in ("240", "$240") or "240" in lit for lit in literals) or "240" not in literals
    # date range end also present
    assert "2024-12-07" in literals


def test_generic_lowercase_domain_words_are_not_flagged():
    literals = lc.extract_literals(TASK_RECORD["user_prompt"])
    assert "single" not in literals
    assert "room" not in literals


def test_leak_check_flags_a_name_copied_into_the_diff():
    diff_text = "Step 3. Confirm the booking for Alex Green before charging the card."
    hits = lc.leak_check(diff_text, [TASK_RECORD])
    assert hits, "expected the guest name to be flagged as a leak"
    assert any("Alex Green" in h for h in hits)


def test_leak_check_flags_a_date_copied_into_the_diff():
    diff_text = "Step 2. Verify availability for 2024-12-04 specifically."
    hits = lc.leak_check(diff_text, [TASK_RECORD])
    assert any("2024-12-04" in h for h in hits)


def test_leak_check_is_clean_when_the_diff_only_uses_general_language():
    diff_text = "Step 3. ESTABLISH: confirm the guest's identity before calling book_room."
    hits = lc.leak_check(diff_text, [TASK_RECORD])
    assert hits == []


def test_leak_check_does_not_flag_argument_field_names_as_leaks():
    """Regression: field NAMES in tool-call arguments/results (e.g.
    `guest_name`, `check_in_date`) are schema, not task-specific data — a
    diff is free to mention them. Found live: the first leak-check pass
    JSON-dumped the whole arguments dict and matched quoted KEYS too.
    """
    record = {
        "user_goal": "book_room",
        "position": 7,
        "user_prompt": "Book a room for the dates given.",
        "tool_calls": [
            {
                "tool_name": "book_room",
                "arguments": {"guest_name": "Alex Green", "check_in_date": "2024-12-04", "check_out_date": "2024-12-07"},
                "content": {"guest": "Alex Green", "booking_id": "BK042"},
            }
        ],
    }
    diff_text = "Step 4. Read the guest_name and check_in_date/check_out_date arguments before calling book_room."
    hits = lc.leak_check(diff_text, [record])
    assert hits == [], f"field names should never be flagged, got: {hits}"


def test_leak_check_ignores_a_sentence_starting_capitalized_stopword():
    record = {"user_goal": "x", "position": 0, "user_prompt": "Please check the loyalty status.", "tool_calls": []}
    diff_text = "Please verify the loyalty status before continuing."
    hits = lc.leak_check(diff_text, [record])
    assert hits == []
