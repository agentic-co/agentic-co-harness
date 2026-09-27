from datetime import date


def _parse_date(value):
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _as_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _price_per_night(rooms, room_type):
    if not isinstance(rooms, dict) or not isinstance(room_type, str):
        return None
    info = rooms.get(room_type)
    if not isinstance(info, dict):
        return None
    return _as_number(info.get("price_per_night"))


def _booking_matches(booking, guest_name, old_in, old_out):
    if not isinstance(booking, dict):
        return False
    guest = booking.get("guest")
    if guest is None:
        guest = booking.get("guest_name")
    if guest != guest_name:
        return False
    b_in = _parse_date(booking.get("check_in_date"))
    b_out = _parse_date(booking.get("check_out_date"))
    return b_in == old_in and b_out == old_out


def check(tool_results: dict, params: dict) -> bool | None:
    if not isinstance(tool_results, dict) or not isinstance(params, dict):
        return None

    guest_name = params.get("guest_name")
    room_type = params.get("room_type")
    amount = _as_number(params.get("amount"))
    if not isinstance(guest_name, str) or not guest_name:
        return None
    if not isinstance(room_type, str) or not room_type:
        return None
    if amount is None:
        return None

    old_in = _parse_date(params.get("old_check_in_date"))
    old_out = _parse_date(params.get("old_check_out_date"))
    new_in = _parse_date(params.get("check_in_date"))
    new_out = _parse_date(params.get("check_out_date"))
    if old_in is None or old_out is None or new_in is None or new_out is None:
        return None

    old_nights = (old_out - old_in).days
    new_nights = (new_out - new_in).days
    if old_nights <= 0 or new_nights <= 0:
        return None

    # Price for the new room type must be known.
    rooms = tool_results.get("show_available_rooms")
    new_price = _price_per_night(rooms, room_type)
    if new_price is None:
        return None

    # Determine the original booking's room type from collected evidence.
    old_room_type = None

    bookings = tool_results.get("internal_get_booking_details")
    if isinstance(bookings, dict):
        for booking in bookings.values():
            if _booking_matches(booking, guest_name, old_in, old_out):
                candidate = booking.get("room_type")
                if isinstance(candidate, str) and candidate:
                    old_room_type = candidate
                    break

    if old_room_type is None:
        info = tool_results.get("find_booking_info")
        if _booking_matches(info, guest_name, old_in, old_out):
            candidate = info.get("room_type")
            if isinstance(candidate, str) and candidate:
                old_room_type = candidate

    if old_room_type is None:
        return None

    # Price for the original room type must also be known.
    old_price = _price_per_night(rooms, old_room_type)
    if old_price is None:
        return None

    old_cost = old_price * old_nights
    new_cost = new_price * new_nights
    difference = new_cost - old_cost

    return amount >= difference
