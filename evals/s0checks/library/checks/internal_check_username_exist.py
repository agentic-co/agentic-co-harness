def check(tool_results: dict, params: dict) -> bool | None:
    """
    Precondition 'internal_check_username_exist':
    True  <=> params['username'] exists as a top-level key in the accounts
              section of the database (per evidence collected so far),
    False <=> evidence establishes it does NOT exist,
    None  <=> evidence is insufficient to decide.
    """
    if not isinstance(tool_results, dict) or not isinstance(params, dict):
        return None

    # The username value at hand must be present to tie evidence to this request.
    username = params.get("username")
    if not isinstance(username, str) or username == "":
        return None

    def _coerce_bool(value):
        """Best-effort defensive parse of a tool's true/false return value."""
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered == "true":
                return True
            if lowered == "false":
                return False
        return None

    # Primary evidence: the dedicated existence-check tool.
    direct = _coerce_bool(tool_results.get("internal_check_username_exist"))
    if direct is not None:
        return direct

    # Fallback evidence: inspect the full database dump's accounts section.
    db = tool_results.get("internal_get_database")
    if not isinstance(db, dict):
        return None

    for section_key, section_value in db.items():
        if (
            isinstance(section_key, str)
            and "account" in section_key.strip().lower()
            and isinstance(section_value, dict)
        ):
            return username in section_value

    # No usable accounts section found in the database dump.
    return None
