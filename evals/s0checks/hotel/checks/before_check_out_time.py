from datetime import datetime


_TIME_FORMATS = ("%H:%M:%S", "%H:%M")
_TIMESTAMP_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M",
)


def _parse_interaction_datetime(value):
    """Parse the interaction timestamp into a datetime. Returns None if malformed."""
    if isinstance(value, dict):
        for key in ("timestamp", "time", "current_time", "interaction_time"):
            if key in value:
                value = value[key]
                break
        else:
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    # Handle trailing 'Z' (UTC marker) for ISO parsing compatibility.
    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(iso_text)
    except ValueError:
        pass
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _parse_time_of_day(value):
    """Parse a check-out time (e.g. '11:00' or '11:00:00') into a time object."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    # Fall back to a full ISO timestamp and use only its time-of-day part.
    try:
        return datetime.fromisoformat(text).time()
    except ValueError:
        return None


def check(tool_results: dict, params: dict):
    """
    Precondition 'before_check_out_time':
    True  iff the current interaction time-of-day is strictly BEFORE check_out_time
          on the interaction date.
    False iff the interaction time-of-day is on or after check_out_time.
    None  if the required evidence is missing or malformed.
    """
    if not isinstance(tool_results, dict) or not isinstance(params, dict):
        return None

    raw_now = tool_results.get("internal_get_interaction_time")
    if raw_now is None:
        return None  # interaction time never collected; cannot decide

    now_dt = _parse_interaction_datetime(raw_now)
    if now_dt is None:
        return None

    raw_check_out_time = params.get("check_out_time")
    if raw_check_out_time is None:
        return None  # check_out_time not resolved for this request

    check_out_t = _parse_time_of_day(raw_check_out_time)
    if check_out_t is None:
        return None

    # Compare time-of-day on the interaction date: strictly before => satisfied.
    return now_dt.time() < check_out_t
