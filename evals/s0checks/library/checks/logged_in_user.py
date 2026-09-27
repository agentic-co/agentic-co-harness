def check(tool_results: dict, params: dict) -> bool | None:
    """
    Deterministic precondition check for 'logged_in_user'.

    Positive condition: the user with params['username'] is logged in
    previously with the correct credentials to perform this action.

    Returns:
        True  -> evidence establishes the user is logged in.
        False -> evidence establishes the user is NOT logged in.
        None  -> evidence is insufficient to decide.
    """
    # Defensive access only; never assume keys exist.
    if not isinstance(tool_results, dict) or not isinstance(params, dict):
        return None

    # The precondition is stated about a specific {username}; if it is
    # missing we cannot evaluate the condition as named.
    username = params.get("username")
    if not isinstance(username, str) or username == "":
        return None

    # The only direct evidence of a credentials-based login is the
    # login_user tool result. Without it we cannot decide.
    # (logout_user returns True even if the user was never logged in,
    # so a logout result alone proves nothing about a prior login.)
    if "login_user" not in tool_results:
        return None

    login_result = tool_results.get("login_user")
    if not isinstance(login_result, bool):
        # Malformed / unexpected login evidence -> insufficient.
        return None

    if login_result is False:
        # The recorded login attempt failed: the user did not log in
        # with correct credentials, so the condition is not satisfied.
        return False

    # login_result is True: a successful credentials-based login occurred.
    # A successful logout (logout_user returns True on success) after that
    # login ends the logged-in state.
    if "logout_user" in tool_results:
        logout_result = tool_results.get("logout_user")
        if isinstance(logout_result, bool) and logout_result is True:
            keys = list(tool_results.keys())
            # Membership already verified above, so index() is safe.
            login_idx = keys.index("login_user")
            logout_idx = keys.index("logout_user")
            if logout_idx > login_idx:
                # Logout happened after the successful login:
                # the user is no longer logged in for this action.
                return False
            # Logout happened before the successful login:
            # the logged-in state holds.

    return True
