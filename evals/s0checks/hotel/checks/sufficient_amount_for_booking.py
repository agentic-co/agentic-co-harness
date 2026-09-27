from datetime import datetime

_DATE_FORMAT = "%Y-%m-%d"
_EPSILON = 1e-9


def _parse_date(value):
    """Parse a YYYY-MM-DD string into a datetime, or None if malformed."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), _DATE_FORMAT)
    except (ValueError, TypeError):
        return None


def _as_number(value):
    """Coerce a value to float, or None if it is not a plain number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def check(tool_results: dict, params: dict) -> bool | None:
    """
    Precondition: sufficient_amount_for_booking

    True  if params["amount"] >= (price_per_night of params["room_type"]) * nights
          between params["check_in_date"] and params["check_out_date"],
          as evidenced by the show_available_rooms tool result.
    False if the amount is strictly less than that total cost.
    None  if any required value is missing or malformed (insufficient evidence).
    """
    if not isinstance(tool_results, dict) or not isinstance(params, dict):
        return None

    room_type = params.get("room_type")
    amount = _as_number(params.get("amount"))
    if amount is None:
        return None

    check_in = _parse_date(params.get("check_in_date"))
    check_out = _parse_date(params.get("check_out_date"))
    if check_in is None or check_out is None:
        return None

    nights = (check_out - check_in).days
    if nights <= 0:
        # Booking cost is not meaningful for a non-positive stay length.
        return None

    # The nightly rate comes from the show_available_rooms tool result.
    availability = tool_results.get("show_available_rooms")
    if not isinstance(availability, dict):
        return None

    room_entry = availability.get(room_type) if isinstance(room_type, str) else None
    if not isinstance(room_entry, dict):
        return None

    price_per_night = _as_number(room_entry.get("price_per_night"))
    if price_per_night is None:
        return None

    total_cost = price_per_night * nights
    return (amount + _EPSILON) >= total_cost
