def check(tool_results: dict, params: dict):
    """
    Precondition: internal_check_room_exist
    Positive: the specified room ID must exist in the database under the 'rooms' section.

    Returns True if evidence establishes the room exists, False if evidence
    establishes it does not exist, and None if the evidence is insufficient.
    """
    if not isinstance(tool_results, dict) or not isinstance(params, dict):
        return None

    # The room_id named in the precondition must be resolved for this request.
    room_id = params.get("room_id")
    if not isinstance(room_id, str):
        return None
    room_id = room_id.strip()
    if not room_id:
        return None

    # Primary evidence: the dedicated existence-check tool.
    result = tool_results.get("internal_check_room_exist")
    if isinstance(result, bool):
        return result
    if isinstance(result, str):
        normalized = result.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False

    # Fallback evidence: a full database snapshot, inspected under 'rooms'.
    db = tool_results.get("internal_get_database")
    if isinstance(db, dict):
        rooms = db.get("rooms")
        if isinstance(rooms, dict):
            keys = {str(k).strip() for k in rooms.keys()}
            return room_id in keys
        if isinstance(rooms, list):
            for entry in rooms:
                if isinstance(entry, dict):
                    candidate = entry.get("room_id")
                    if candidate is None:
                        candidate = entry.get("id")
                    if isinstance(candidate, str) and candidate.strip() == room_id:
                        return True
                elif isinstance(entry, str) and entry.strip() == room_id:
                    return True
            return False
        # 'rooms' section missing or of unexpected shape -> cannot decide.
        return None

    # No usable evidence was collected.
    return None
