def _norm_text(value):
    """Normalize a name/id string for comparison; None if not a usable string."""
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split()).casefold()
    return collapsed if collapsed else None


def check(tool_results: dict, params: dict) -> bool | None:
    """
    Precondition: guest_already_checked_in
    Positive: the guest "{guest_name}" must be listed in the room check-in records.
    """
    if not isinstance(params, dict):
        return None

    target = _norm_text(params.get("guest_name"))
    if not target:
        return None  # guest_name missing or unusable

    if not isinstance(tool_results, dict):
        return None

    records = tool_results.get("internal_get_room_checkin_details")
    if not isinstance(records, dict):
        return None  # check-in records never fetched (or malformed)

    # Build booking_id -> guest name mapping if booking details were fetched.
    # Needed because check-in records reference guests via booking_id.
    booking_guests = {}
    bookings = tool_results.get("internal_get_booking_details")
    if isinstance(bookings, dict):
        for bid, info in bookings.items():
            if not isinstance(info, dict):
                continue
            guest = info.get("guest")
            if not isinstance(guest, str):
                guest = info.get("guest_name")
            guest_norm = _norm_text(guest)
            bid_norm = _norm_text(bid)
            if guest_norm is not None and bid_norm is not None:
                booking_guests[bid_norm] = guest_norm

    for room_key, record in records.items():
        # Defensive: some systems may key check-in records directly by guest name.
        if _norm_text(room_key) == target:
            return True

        if not isinstance(record, dict):
            return None  # record malformed; cannot attribute it

        # 1) Record may carry the guest's name directly.
        owner = None
        for key in ("guest", "guest_name"):
            owner = _norm_text(record.get(key))
            if owner is not None:
                break

        # 2) Otherwise attribute the record via its booking_id.
        if owner is None:
            bid_norm = _norm_text(record.get("booking_id"))
            if bid_norm is None:
                return None  # record lists neither guest name nor booking_id
            if bid_norm not in booking_guests:
                return None  # cannot resolve whose check-in record this is
            owner = booking_guests[bid_norm]

        if owner == target:
            return True

    # Every check-in record was attributable and none belongs to the guest.
    return False
