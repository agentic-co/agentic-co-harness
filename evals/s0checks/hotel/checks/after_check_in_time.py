from datetime import datetime


def _parse_timestamp(value):
    """Parse a system timestamp string into a datetime, or None if malformed."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _parse_time_of_day(value):
    """Parse a time-of-day string (e.g. '15:00') into a time, or None if malformed."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in ("%H:%M:%S", "%H:%M", "%I:%M:%S %p", "%I:%M %p"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def check(tool_results: dict, params: dict) -> bool | None:
    """
    Precondition 'after_check_in_time':
    The current interaction time-of-day must be on or after check_in_time
    on the interaction date.

    Returns True if satisfied, False if not satisfied, or None if the
    evidence is insufficient to decide.
    """
    # Need the current interaction time from the internal tool; if it was
    # never called (absent) or malformed, we cannot decide.
    current_dt = _parse_timestamp(tool_results.get("internal_get_interaction_time"))
    if current_dt is None:
        return None

    # Need the concrete check-in time value for this request.
    check_in_t = _parse_time_of_day(params.get("check_in_time"))
    if check_in_t is None:
        return None

    # Compare time-of-day of the interaction against the check-in time:
    # condition holds when interaction time is on or after check_in_time.
    return current_dt.time() >= check_in_t
