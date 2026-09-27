from datetime import date

_CONFIRMED = "confirmed"


def _norm_text(value):
    """Normalize a string for comparison, or None if not a string."""
    if not isinstance(value, str):
        return None
    return value.strip().casefold()


def _norm_date(value):
    """Canonicalize a YYYY-MM-DD date string; falls back to stripped string."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        return s


def _matches_guest_and_dates(booking, guest_key, ci, co):
    """
    Return True/False if the booking's guest and date range definitively
    match or do not match; None if the entry is too malformed to tell.
    """
    if not isinstance(booking, dict):
        return None
    b_guest = _norm_text(booking.get("guest"))
    b_ci = _norm_date(booking.get("check_in_date"))
    b_co = _norm_date(booking.get("check_out_date"))
    if b_guest is None or b_ci is None or b_co is None:
        return None
    return b_guest == guest_key and b_ci == ci and b_co == co


def _is_confirmed(booking):
    status = booking.get("status")
    return isinstance(status, str) and status.strip().casefold() == _CONFIRMED


def check(tool_results: dict, params: dict) -> bool | None:
    if not isinstance(tool_results, dict) or not isinstance(params, dict):
        return None

    guest_key = _norm_text(params.get("guest_name"))
    ci = _norm_date(params.get("check_in_date"))
    co = _norm_date(params.get("check_out_date"))
    if guest_key is None or ci is None or co is None:
        return None

    # Primary evidence: the full listing of all bookings in the system.
    bookings = tool_results.get("internal_get_booking_details")
    if isinstance(bookings, dict):
        undecided = False
        for booking in bookings.values():
            match = _matches_guest_and_dates(booking, guest_key, ci, co)
            if match is None:
                # Malformed entry: could in principle hide a matching
                # confirmed reservation, so remember the ambiguity.
                undecided = True
                continue
            if not match:
                continue
            if _is_confirmed(booking):
                return True
            status = booking.get("status")
            if not isinstance(status, str):
                # Matching reservation exists but its status is unreadable.
                undecided = True
            # Non-confirmed status on a matching reservation: keep looking
            # in case another reservation also matches.
        if undecided:
            return None
        return False

    # Fallback evidence: a single booking lookup result, usable only if it
    # actually corresponds to this guest and date range.
    candidate = tool_results.get("find_booking_info")
    if isinstance(candidate, dict):
        match = _matches_guest_and_dates(candidate, guest_key, ci, co)
        if match:
            status = candidate.get("status")
            if isinstance(status, str):
                return status.strip().casefold() == _CONFIRMED
            return None
        return None

    # Insufficient evidence to decide.
    return None
