import re
from datetime import date

_DATE_PREFIX_RE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")


def _parse_date(value):
    """Parse a leading YYYY-MM-DD date from a string. Returns a date or None."""
    if not isinstance(value, str):
        return None
    m = _DATE_PREFIX_RE.match(value)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def check(tool_results: dict, params: dict) -> bool | None:
    """
    Precondition: is_booking_date_within_lead_time_range

    TRUE when check_in_date is no earlier than min_booking_lead_time_days days
    after, and no later than max_booking_lead_time_days days after, the current
    interaction date. Returns None when evidence is insufficient.
    """
    if not isinstance(tool_results, dict) or not isinstance(params, dict):
        return None

    # Current interaction date must come from the interaction-time tool call.
    ts = tool_results.get("internal_get_interaction_time")
    if ts is None:
        return None
    current_date = _parse_date(ts)
    if current_date is None:
        return None

    # The check-in date under evaluation.
    check_in = _parse_date(params.get("check_in_date"))
    if check_in is None:
        return None

    # Lead-time bounds must be concrete numbers.
    min_lead = params.get("min_booking_lead_time_days")
    max_lead = params.get("max_booking_lead_time_days")
    if isinstance(min_lead, bool) or not isinstance(min_lead, (int, float)):
        return None
    if isinstance(max_lead, bool) or not isinstance(max_lead, (int, float)):
        return None

    lead_days = (check_in - current_date).days

    if lead_days < min_lead:
        return False
    if lead_days > max_lead:
        return False
    return True
