def check(tool_results: dict, params: dict) -> bool | None:
    """
    Precondition 'within_borrow_limit':
    The user {username} must have strictly less than {borrow_limit} books
    in their 'borrowed' collection.

    Evidence sources (either suffices, both preferred):
      - internal_get_user_num_borrowed -> int count of borrowed books
      - internal_get_user_borrowed     -> list of borrowed book ids

    Returns True/False when decidable, None otherwise.
    """
    if not isinstance(tool_results, dict) or not isinstance(params, dict):
        return None

    def _as_number(value):
        # Coerce a value to a float in a strict, deterministic way.
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except (ValueError, AttributeError):
                return None
        return None

    # Required threshold from params.
    limit = _as_number(params.get("borrow_limit"))
    if limit is None:
        return None

    # Count from the dedicated counter tool, if available and well-formed.
    count = None
    num_result = tool_results.get("internal_get_user_num_borrowed")
    if isinstance(num_result, int) and not isinstance(num_result, bool):
        if num_result >= 0:  # negative borrowed counts are malformed evidence
            count = float(num_result)

    # Fallback / cross-check: derive count from the borrowed-ids list.
    list_count = None
    borrowed_result = tool_results.get("internal_get_user_borrowed")
    if isinstance(borrowed_result, list):
        list_count = float(len(borrowed_result))

    # Conflicting well-formed evidence -> cannot decide safely.
    if count is not None and list_count is not None and count != list_count:
        return None

    if count is None:
        count = list_count

    if count is None:
        # Neither tool provided usable evidence.
        return None

    # Positive condition: strictly fewer borrowed books than the limit.
    return count < limit
