def check(tool_results: dict, params: dict) -> bool | None:
    """
    Precondition "internal_is_restricted":
    True  iff the book named by params['book_title'] is marked restricted (true),
    False iff it is marked not restricted (false),
    None  when the evidence is missing or malformed.
    """
    if not isinstance(params, dict):
        return None

    book_title = params.get("book_title")
    if not isinstance(book_title, str) or not book_title.strip():
        return None

    if not isinstance(tool_results, dict):
        return None

    restricted = tool_results.get("internal_is_restricted")

    # Tool never called -> insufficient evidence.
    if restricted is None:
        return None

    # Parsed JSON booleans.
    if isinstance(restricted, bool):
        return restricted

    # Defensive: tolerate string-encoded booleans.
    if isinstance(restricted, str):
        normalized = restricted.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
        return None

    # Defensive: tolerate integer-encoded booleans.
    if isinstance(restricted, int) and not isinstance(restricted, bool):
        if restricted == 1:
            return True
        if restricted == 0:
            return False
        return None

    return None
