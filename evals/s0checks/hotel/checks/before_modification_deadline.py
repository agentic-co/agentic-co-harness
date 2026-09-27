from datetime import datetime, timedelta, timezone

_TIMESTAMP_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
)

_TIME_FORMATS = (
    "%H:%M:%S",
    "%H:%M",
)


def _parse_timestamp(value):
    """Parse a system timestamp string into a naive datetime (UTC-normalized)."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in _TIMESTAMP_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _parse_date(value):
    """Parse a 'YYYY-MM-DD' date string into a datetime at midnight."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d")
    except ValueError:
        return None


def _parse_time(value):
    """Parse an 'HH:MM' or 'HH:MM:SS' time string into a time object."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def _to_number(value):
    """Coerce a value to a finite float, or None if not numeric."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def check(tool_results: dict, params: dict):
    # Evidence: current system time must have been fetched.
    current = _parse_timestamp(
        tool_results.get("internal_get_interaction_time")
        if isinstance(tool_results, dict)
        else None
    )
    if current is None:
        return None

    if not isinstance(params, dict):
        return None

    # Required params: check_in_date, check_in_time, modification_deadline_hours.
    check_in_date = _parse_date(params.get("check_in_date"))
    if check_in_date is None:
        return None

    check_in_time = _parse_time(params.get("check_in_time"))
    if check_in_time is None:
        return None

    deadline_hours = _to_number(params.get("modification_deadline_hours"))
    if deadline_hours is None:
        return None

    # Absolute check-in moment on the given date.
    check_in_dt = check_in_date.replace(
        hour=check_in_time.hour,
        minute=check_in_time.minute,
        second=check_in_time.second,
        microsecond=0,
    )

    # Deadline: modification_deadline_hours hours before check-in.
    deadline = check_in_dt - timedelta(hours=deadline_hours)

    # Positive condition: current time is NO LATER than the deadline (inclusive).
    return current <= deadline
