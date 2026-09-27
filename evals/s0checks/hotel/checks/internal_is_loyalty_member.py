def check(tool_results: dict, params: dict) -> bool | None:
    """
    Precondition "internal_is_loyalty_member":
    The guest "{guest_name}" must be enrolled in the hotel's loyalty program.

    Returns True if evidence establishes enrollment, False if evidence establishes
    non-enrollment, and None if the evidence is insufficient to decide.
    """
    if not isinstance(params, dict) or not isinstance(tool_results, dict):
        return None

    guest_name = params.get("guest_name")
    if not isinstance(guest_name, str) or not guest_name.strip():
        # Cannot tie any evidence to a specific guest.
        return None

    guest_key = guest_name.strip().lower()

    # Primary evidence: the direct membership check tool.
    membership = tool_results.get("internal_is_loyalty_member")
    if isinstance(membership, bool):
        return membership

    # Secondary evidence: the loyalty member info lookup for this guest.
    info = tool_results.get("internal_get_loyalty_member_info")
    if isinstance(info, dict):
        if not info:
            # A member lookup returned no record for the guest -> not enrolled.
            return False
        membership_keys = {"status", "points", "tier", "member_id", "loyalty_id"}
        keys = {str(k).strip().lower() for k in info.keys()}
        values = {
            str(v).strip().lower()
            for v in info.values()
            if isinstance(v, str)
        }
        if (keys & membership_keys) or (guest_key in values):
            # A non-empty membership record establishes enrollment.
            return True
        # Malformed/ambiguous record -> insufficient evidence.
        return None
    if isinstance(info, str):
        lowered = info.lower()
        if any(
            phrase in lowered
            for phrase in ("not found", "not a member", "not enrolled", "no record")
        ):
            return False
        return None

    # No usable evidence was collected.
    return None
