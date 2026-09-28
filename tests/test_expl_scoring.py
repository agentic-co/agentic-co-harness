"""`scoring.is_refusal` (fixed 2026-09-28 — the original `num_function_calls
== 0` proxy read 0.0 on every split and every version, because this ASOP's
gate always requires at least one VERIFY call before any refusal). Pure
synthetic records, no SOPBench needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parents[1] / "scripts" / "eval"
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

from expl import scoring  # noqa: E402


def _rec(user_goal: str, tool_names: list[str], **overrides) -> dict:
    base = {
        "user_goal": user_goal,
        "tool_calls": [{"tool_name": t} for t in tool_names],
        "success": True,
        "action_should_succeed": True,
        "num_function_calls": len(tool_names),
    }
    base.update(overrides)
    return base


def test_is_refusal_true_when_the_goal_action_is_never_called():
    # VERIFY calls only ("show_available_rooms"), the action itself
    # ("book_room") never attempted — a correct or incorrect refusal either
    # way, but a refusal either way.
    rec = _rec("book_room", ["show_available_rooms", "internal_get_booking_details"])
    assert scoring.is_refusal(rec) is True


def test_is_refusal_false_when_the_goal_action_is_called():
    rec = _rec("book_room", ["show_available_rooms", "book_room"])
    assert scoring.is_refusal(rec) is False


def test_is_refusal_is_not_simply_zero_tool_calls():
    """Regression for the original bug: a task with ONE tool call that is
    NOT the goal action must still count as a refusal (min tool calls
    measured live was 1, never 0 — a `== 0` check silently answered "never"
    on every real task).
    """
    rec = _rec("cancel_reservation", ["internal_get_booking_details"])
    assert rec["num_function_calls"] == 1
    assert scoring.is_refusal(rec) is True


def test_summarize_refusal_rate_is_not_a_constant_zero_on_a_mixed_set():
    records = {
        0: _rec("book_room", ["show_available_rooms", "book_room"], action_should_succeed=True),
        1: _rec("book_room", ["show_available_rooms"], action_should_succeed=False, success=True),
        2: _rec("cancel_reservation", ["internal_get_booking_details"], action_should_succeed=False, success=True),
    }
    summary = scoring.summarize(records)
    assert summary["refusal_rate"] == 2 / 3
