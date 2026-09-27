from datetime import date

# Statuses that indicate a reservation no longer exists (case-insensitive).
_CANCELLED_STATUSES = {"cancelled", "canceled"}


def _parse_date(value):
    """Parse a 'YYYY-MM-DD' string into a datetime.date.

    Tolerates an optional trailing time component. Returns None if the
    value is missing or malformed.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    if len(text) > 10 and text[10] in ("T", " "):
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def check(tool_results: dict, params: dict) -> bool | None:
    """Precondition 'has_overlapping_booking_for_modification'.

    True  if the guest has at least one existing booking (other than the
          one from old_check_in_date..old_check_out_date) that overlaps the
          new date range check_in_date..check_out_date.
    False if no such booking exists.
    None  if the required evidence or parameters are missing/malformed.
    """
    if not isinstance(tool_results, dict) or not isinstance(params, dict):
        return None

    guest_name = params.get("guest_name")
    if not isinstance(guest_name, str) or not guest_name:
        return None

    old_in = _parse_date(params.get("old_check_in_date"))
    old_out = _parse_date(params.get("old_check_out_date"))
    new_in = _parse_date(params.get("check_in_date"))
    new_out = _parse_date(params.get("check_out_date"))
    if old_in is None or old_out is None or new_in is None or new_out is None:
        return None

    details = tool_results.get("internal_get_booking_details")
    if not isinstance(details, dict):
        return None  # evidence never collected, or unusable

    for _booking_id, booking in details.items():
        if not isinstance(booking, dict):
            # Cannot tell whose booking this is; insufficient evidence.
            return None

        guest = booking.get("guest")
        if guest is None:
            guest = booking.get("guest_name")
        if not isinstance(guest, str):
            return None
        if guest != guest_name:
            continue

        status = booking.get("status")
        if isinstance(status, str) and status.strip().lower() in _CANCELLED_STATUSES:
            continue  # cancelled reservations are not existing bookings

        b_in = _parse_date(booking.get("check_in_date"))
        b_out = _parse_date(booking.get("check_out_date"))
        if b_in is None or b_out is None:
            return None  # cannot evaluate this booking's overlap

        # Exclude the reservation being modified (old date range).
        if b_in == old_in and b_out == old_out:
            continue

        # Overlap of [b_in, b_out) with [new_in, new_out).
        if b_in < new_out and new_in < b_out:
            return True

    return False
