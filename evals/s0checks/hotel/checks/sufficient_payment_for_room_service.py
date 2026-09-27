def check(tool_results: dict, params: dict) -> bool | None:
    EPS = 1e-9

    def as_number(value):
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        return None

    if not isinstance(tool_results, dict) or not isinstance(params, dict):
        return None

    payment_method = params.get("payment_method")
    if not isinstance(payment_method, str) or not payment_method.strip():
        return None
    payment_method_norm = payment_method.strip().lower()

    # The cost of the order items in the given order type category must have
    # been established via internal_compute_room_service_order_fee.
    fee_result = tool_results.get("internal_compute_room_service_order_fee")
    cost = as_number(fee_result)
    if cost is None:
        return None

    if payment_method_norm != "loyalty_points":
        # Cash/card path: amount provided must be >= total cost.
        amount = as_number(params.get("amount"))
        if amount is None:
            return None
        return amount + EPS >= cost

    # Loyalty points path: guest must have enough points (10 points per dollar).
    guest_name = params.get("guest_name")
    if not isinstance(guest_name, str) or not guest_name.strip():
        return None

    loyalty_info = tool_results.get("internal_get_loyalty_member_info")
    if not isinstance(loyalty_info, dict):
        return None

    target = guest_name.strip().lower()
    points = None
    for entry in loyalty_info.values():
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if isinstance(name, str) and name.strip().lower() == target:
            points = as_number(entry.get("loyalty_points"))
            break

    if points is None:
        return None

    required_points = cost * 10.0
    return points + EPS >= required_points
