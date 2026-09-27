from __future__ import annotations

from datetime import datetime


def _parse_date(value):
    """Parse a 'YYYY-MM-DD' string into a date. Returns None if malformed."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _booking_guest_name(booking: dict):
    """Extract the guest name field from a booking record, if present as a string."""
    for key in ("guest", "guest_name"):
        value = booking.get(key)
        if isinstance(value, str):
            return value
    return None


def check(tool_results: dict, params: dict) -> bool | None:
    """
    Precondition: has_overlapping_booking_for_booking

    TRUE when the guest given in params has at least one existing booking
    (in the records returned by internal_get_booking_details) whose date
    range overlaps the new date range given in params.

    Returns True (condition satisfied), False (condition not satisfied),
    or None (insufficient evidence to decide).
    """
    if not isinstance(tool_results, dict) or not isinstance(params, dict):
        return None

    guest_name = params.get("guest_name")
    if not isinstance(guest_name, str) or not guest_name.strip():
        return None

    new_in = _parse_date(params.get("check_in_date"))
    new_out = _parse_date(params.get("check_out_date"))
    if new_in is None or new_out is None:
        return None

    bookings = tool_results.get("internal_get_booking_details")
    if bookings is None:
        return None
    if not isinstance(bookings, dict):
        return None

    guest_key = guest_name.strip()
    undecidable_booking_for_guest = False

    for booking in bookings.values():
        if not isinstance(booking, dict):
            continue

        b_guest = _booking_guest_name(booking)
        if b_guest is None:
            continue
        if b_guest.strip() != guest_key:
            continue

        # A cancelled reservation is not an "existing" booking.
        status = booking.get("status")
        if isinstance(status, str) and "cancel" in status.strip().lower():
            continue

        b_in = _parse_date(booking.get("check_in_date"))
        b_out = _parse_date(booking.get("check_out_date"))
        if b_in is None or b_out is None:
            # This guest's booking has malformed dates: overlap unknowable.
            undecidable_booking_for_guest = True
            continue

        # Half-open interval overlap: [b_in, b_out) vs [new_in, new_out)
        if b_in < new_out and new_in < b_out:
            return True

    if undecidable_booking_for_guest:
        return None

    return False
