def _as_slot_set(value):
    """Normalize a collection of slots (or a dict whose keys are slots)
    into a set of slots. Raises TypeError if elements are unhashable."""
    if isinstance(value, dict):
        value = list(value.keys())
    return {s.strip() if isinstance(s, str) else s for s in value}


def check(tool_results: dict, params: dict) -> bool | None:
    tool_results = tool_results if isinstance(tool_results, dict) else {}
    params = params if isinstance(params, dict) else {}

    # Primary evidence: the dedicated availability-check tool's boolean result.
    direct = tool_results.get("internal_all_slots_available_for_the_room_on_the_date")
    if isinstance(direct, bool):
        return direct
    if isinstance(direct, int) and direct in (0, 1):
        return bool(direct)

    # Required parameter values for this specific request.
    room_id = params.get("room_id")
    resv_date = params.get("resv_date")
    slots = params.get("slots")

    if not isinstance(room_id, str) or not room_id:
        return None
    if not isinstance(resv_date, str) or not resv_date:
        return None
    if not isinstance(slots, list):
        return None

    # No slots requested: vacuously, all (zero) requested slots are available.
    if not slots:
        return True

    # Fallback evidence: the room availability listing returned by
    # show_available_rooms (rooms -> dates -> available slots).
    rooms = tool_results.get("show_available_rooms")
    if not isinstance(rooms, dict):
        return None

    room_entry = rooms.get(room_id)
    if not isinstance(room_entry, dict):
        # Room is not listed as having any availability at all.
        return False

    date_entry = room_entry.get(resv_date)
    if date_entry is None:
        # The requested date is not listed as having any availability.
        return False

    try:
        available = _as_slot_set(date_entry)
        requested = _as_slot_set(slots)
    except TypeError:
        return None

    # Condition holds iff every requested slot is among the available slots.
    return requested.issubset(available)
