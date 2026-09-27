from __future__ import annotations


def _as_number(value):
    """Return the value as a float if it is a genuine number, else None.

    Booleans are rejected (bool is a subclass of int in Python), and any
    non-numeric value is treated as malformed/missing.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def check(tool_results: dict, params: dict) -> bool | None:
    tool_results = tool_results if isinstance(tool_results, dict) else {}
    params = params if isinstance(params, dict) else {}

    # The precondition is stated about a specific user; we must know which one.
    username = params.get("username")
    if not isinstance(username, str) or not username:
        return None

    # --- Evidence for the user's account balance ---------------------------
    # Primary source: get_account_balance. Note the tool may legitimately
    # return None ("retrieval conditions not met"), which means we cannot
    # decide and must not guess.
    balance = _as_number(tool_results.get("get_account_balance"))
    if balance is None:
        # Fallback: balance supplied directly in params.
        balance = _as_number(params.get("balance"))
    if balance is None:
        return None  # insufficient evidence about the balance

    # --- Evidence for the late fee -----------------------------------------
    # The late fee is defined as: user's late_book_count * late_fee_per_book
    # (from the database). internal_calculate_late_fee computes exactly this
    # product from the user's late returns.
    fee = _as_number(tool_results.get("internal_calculate_late_fee"))
    if fee is None:
        # Fallback: compute the product from the referenced values in params.
        late_book_count = _as_number(params.get("late_book_count"))
        late_fee_per_book = _as_number(params.get("late_fee_per_book"))
        if late_book_count is not None and late_fee_per_book is not None:
            fee = late_book_count * late_fee_per_book
    if fee is None:
        return None  # insufficient evidence about the late fee

    # Positive condition: the balance is strictly MORE than the late fee.
    return balance > fee
