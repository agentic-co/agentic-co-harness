def check(tool_results: dict, params: dict) -> bool | None:
    """
    Precondition 'within_max_reservation_slots':
    The user "{username}" must have a total number of reserved slots less than or
    equal to {max_reservation_slots}, where the total is the sum of:
      - their currently reserved slots (evidence: internal_get_num_reserved_slots), and
      - the number of newly requested slots (params["slots"]).

    Returns True if established, False if violated, None if evidence is insufficient.
    """

    def as_number(value):
        """Defensively coerce a value to a number; None if not numeric."""
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
        return None

    # Defensive: ensure we have dicts to work with.
    if not isinstance(tool_results, dict) or not isinstance(params, dict):
        return None

    # The condition is about a specific user; username must be present.
    username = params.get("username")
    if not isinstance(username, str) or not username:
        return None

    # Newly requested slots must be provided as a list/tuple of slots.
    slots = params.get("slots")
    if not isinstance(slots, (list, tuple)):
        return None
    new_slot_count = len(slots)

    # The threshold must be present and numeric.
    max_slots = as_number(params.get("max_reservation_slots"))
    if max_slots is None:
        return None

    # Evidence: the user's currently reserved slot count must have been collected.
    reserved_raw = tool_results.get("internal_get_num_reserved_slots")
    reserved_count = as_number(reserved_raw)
    if reserved_count is None:
        return None

    total_reserved = reserved_count + new_slot_count
    return total_reserved <= max_slots
