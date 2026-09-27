from datetime import datetime, timedelta
from typing import Optional

_DATE_FORMAT = "%Y-%m-%d"


def _parse_date(value):
    """Parse a 'YYYY-MM-DD' string into a datetime.date. Return None if malformed."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), _DATE_FORMAT).date()
    except ValueError:
        return None


def check(tool_results: dict, params: dict) -> Optional[bool]:
    """
    Precondition 'room_type_available_for_dates':
    The given room_type must have at least one specific room available for
    every date from check_in_date up to (but not including) check_out_date.
    """
    if not isinstance(params, dict):
        return None
    if not isinstance(tool_results, dict):
        return None

    room_type = params.get("room_type")
    check_in_raw = params.get("check_in_date")
    check_out_raw = params.get("check_out_date")

    if not isinstance(room_type, str) or not room_type:
        return None

    check_in = _parse_date(check_in_raw)
    check_out = _parse_date(check_out_raw)
    if check_in is None or check_out is None:
        return None

    # Required dates: check_in inclusive, check_out exclusive.
    required_dates = set()
    d = check_in
    one_day = timedelta(days=1)
    while d < check_out:
        required_dates.add(d)
        d = d + one_day

    availability_data = tool_results.get("show_available_rooms")
    if availability_data is None:
        # Tool never called: insufficient evidence.
        return None
    if not isinstance(availability_data, dict):
        return None

    if room_type not in availability_data:
        # The listing covers all room types; an absent room type means no
        # room of that type is available on any date.
        return False

    type_entry = availability_data[room_type]
    if not isinstance(type_entry, dict):
        return None

    rooms = type_entry.get("availability")
    if not isinstance(rooms, dict):
        return None

    for room_id, room_dates in rooms.items():
        if not isinstance(room_dates, (list, tuple, set)):
            continue
        available_dates = set()
        for raw in room_dates:
            parsed = _parse_date(raw)
            if parsed is not None:
                available_dates.add(parsed)
        if required_dates.issubset(available_dates):
            return True

    return False
