def check(tool_results: dict, params: dict) -> bool | None:
    """
    Precondition: internal_valid_room_change_reason
    The given "reason" must be listed as one of the hotel's accepted reasons
    for requesting a room change.

    Returns True if established, False if not, None if evidence is insufficient.
    """
    if not isinstance(tool_results, dict) or not isinstance(params, dict):
        return None

    # The reason value must be present in params.
    reason = params.get("reason")
    if not isinstance(reason, str):
        return None
    reason_norm = reason.strip().lower()
    if not reason_norm:
        return None

    def _entry_matches(entry) -> bool:
        """Deterministically compare one listed reason against the params reason."""
        if isinstance(entry, str):
            return entry.strip().lower() == reason_norm
        if isinstance(entry, dict):
            val = entry.get("reason")
            if isinstance(val, str):
                return val.strip().lower() == reason_norm
        return False

    # Preferred evidence: the direct validity-check tool's parsed result.
    direct = tool_results.get("internal_valid_room_change_reason")
    if isinstance(direct, bool):
        return direct

    # Fallback evidence: the list of accepted room change reasons.
    options = tool_results.get("show_room_change_options")
    if isinstance(options, list):
        return any(_entry_matches(entry) for entry in options)

    # No sufficient evidence available.
    return None
