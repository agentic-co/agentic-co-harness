def check(tool_results: dict, params: dict) -> bool | None:
    """
    Precondition: 'internal_is_admin'
    TRUE when the user {username} has an 'admin' value of true in the database.

    Evidence comes from the 'internal_is_admin' tool, which returns true/false
    based on whether the user has admin privileges.

    Returns:
        True  - evidence establishes the user IS an admin,
        False - evidence establishes the user is NOT an admin,
        None  - insufficient evidence to decide.
    """
    # Defensive access: never assume inputs are present or well-formed.
    if not isinstance(tool_results, dict) or not isinstance(params, dict):
        return None

    # The precondition references {username}; it must be resolved for this
    # specific request. If it is missing/empty, we cannot tie the evidence
    # to a user, so prefer None over guessing.
    username = params.get("username")
    if not isinstance(username, str) or not username.strip():
        return None

    # The 'internal_is_admin' tool directly answers this precondition.
    # If it was never called, the key is simply absent -> None.
    result = tool_results.get("internal_is_admin")

    # Parsed return value should be a boolean (true/false per the tool schema).
    if isinstance(result, bool):
        return result

    # Any other shape is malformed evidence for this precondition.
    return None
