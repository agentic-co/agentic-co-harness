from datetime import datetime

# Common input formats we try to normalize to ISO 'YYYY-MM-DD' before matching
# against the database's date keys (which are ISO strings).
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%d %B %Y",
    "%B %d %Y",
    "%B %d, %Y",
    "%d %B, %Y",
    "%d %b %Y",
    "%b %d, %Y",
)


def _coerce_bool(value):
    """Best-effort strict-ish boolean coercion of a parsed tool result."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low == "true":
            return True
        if low == "false":
            return False
    return None


def _date_variants(value):
    """Return the set of candidate strings under which the date might be listed
    (the raw value plus its ISO-normalized form, if parseable)."""
    variants = set()
    if isinstance(value, str):
        v = value.strip()
        if v:
            variants.add(v)
            for fmt in _DATE_FORMATS:
                try:
                    variants.add(datetime.strptime(v, fmt).strftime("%Y-%m-%d"))
                    break
                except ValueError:
                    continue
    return variants


def _date_listed(rooms, room_id, resv_date):
    """Decide whether any variant of resv_date is listed under rooms[room_id].

    Returns True/False, or None if the rooms structure is malformed/unusable.
    """
    if not isinstance(rooms, dict):
        return None
    if room_id not in rooms:
        # The room itself is not listed at all, so the date cannot be listed
        # under it.
        return False
    room = rooms[room_id]
    if not isinstance(room, dict):
        return None  # malformed entry; cannot decide
    variants = _date_variants(resv_date)
    for v in variants:
        if v in room:
            return True
    return False


def check(tool_results: dict, params: dict) -> bool | None:
    if not isinstance(params, dict):
        return None

    room_id = params.get("room_id")
    resv_date = params.get("resv_date")
    if not isinstance(room_id, str) or not room_id.strip():
        return None
    if not isinstance(resv_date, str) or not resv_date.strip():
        return None
    room_id = room_id.strip()
    resv_date = resv_date.strip()

    if not isinstance(tool_results, dict):
        return None

    # Primary evidence: the dedicated availability-check tool's own verdict.
    direct = _coerce_bool(tool_results.get("internal_check_date_available_for_the_room"))
    if direct is not None:
        return direct

    # Fallback evidence: a full database dump containing a 'rooms' section,
    # where the room's available dates are listed as keys.
    db = tool_results.get("internal_get_database")
    if isinstance(db, dict):
        result = _date_listed(db.get("rooms"), room_id, resv_date)
        if result is not None:
            return result

    # Fallback evidence: a listing of rooms with their available slots,
    # shaped as {room_id: {date: [slots]}}.
    avail = tool_results.get("show_available_rooms")
    if isinstance(avail, dict):
        result = _date_listed(avail, room_id, resv_date)
        if result is not None:
            return result

    # Insufficient evidence to decide.
    return None
