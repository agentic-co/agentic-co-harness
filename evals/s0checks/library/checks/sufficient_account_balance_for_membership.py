def check(tool_results: dict, params: dict) -> bool | None:
    """
    Precondition: "sufficient_account_balance_for_membership"

    TRUE when the user's account balance is strictly GREATER THAN the
    monthly restricted-access (membership) fee in the database.

    Evidence sources (in priority order):
      - balance: tool_results["get_account_balance"] (float, or None if
        retrieval conditions not met), else params["balance"]
      - fee:     tool_results["internal_get_membership_fee"], else
        params["membership_monthly_fee"]

    Returns True / False when decidable, None otherwise.
    """

    def _to_number(value):
        """Coerce a value to float; return None if not a usable number."""
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except (TypeError, ValueError):
                return None
        return None

    # Defensive access
    if not isinstance(tool_results, dict):
        tool_results = {}
    if not isinstance(params, dict):
        params = {}

    # The condition is stated about a specific user; without a username we
    # cannot attribute the balance evidence to anyone.
    username = params.get("username")
    if not isinstance(username, str) or not username:
        return None

    # Account balance: prefer the evidence collected via get_account_balance.
    balance = _to_number(tool_results.get("get_account_balance"))
    if balance is None:
        # Fall back to a resolved param value if the tool was never called.
        balance = _to_number(params.get("balance"))
    if balance is None:
        # Balance unknown (tool not called, retrieval failed, or malformed).
        return None

    # Membership monthly fee: prefer the database value fetched via
    # internal_get_membership_fee.
    fee = _to_number(tool_results.get("internal_get_membership_fee"))
    if fee is None:
        fee = _to_number(params.get("membership_monthly_fee"))
    if fee is None:
        # Fee unknown.
        return None

    # Positive description: balance is strictly MORE than the monthly fee.
    return balance > fee
