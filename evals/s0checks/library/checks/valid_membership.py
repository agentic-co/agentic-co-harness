from datetime import datetime, date

# Sentinel distinguishing "evidence not found" from "evidence found but null"
_MISSING = object()

# String values that should be interpreted as a null/absent membership field
_NULL_STRINGS = {"", "none", "null", "n/a", "na"}


def _to_datetime(value):
    """Best-effort parse of a date/datetime-like value into a datetime.

    Returns None if the value is not a recognizable date/datetime.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    candidate = s
    if candidate.endswith(("Z", "z")):
        candidate = candidate[:-1] + "+00:00"
    if "T" not in candidate and " " in candidate:
        candidate = candidate.replace(" ", "T", 1)
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%m/%d/%Y",
        "%d %B %Y",
        "%B %d, %Y",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _is_null_membership(raw):
    """True if the raw membership value clearly represents null/absent."""
    if raw is None:
        return True
    if isinstance(raw, str) and raw.strip().lower() in _NULL_STRINGS:
        return True
    return False


def _membership_from_database(db, username):
    """Attempt to read the user's 'membership' field from the full database dump.

    Returns the raw value, or _MISSING if it cannot be located.
    """
    if not isinstance(db, dict) or not username:
        return _MISSING

    # Keyed containers: {username: profile}
    containers = []
    for key in ("users", "profiles", "accounts", "user_profiles", "members", "user_data"):
        value = db.get(key)
        if isinstance(value, dict):
            containers.append(value)
        elif isinstance(value, list):
            for item in value:
                if (
                    isinstance(item, dict)
                    and item.get("username") == username
                    and "membership" in item
                ):
                    return item.get("membership")
    containers.append(db)

    for container in containers:
        profile = container.get(username)
        if isinstance(profile, dict) and "membership" in profile:
            return profile.get("membership")

    return _MISSING


def check(tool_results: dict, params: dict) -> bool | None:
    if not isinstance(tool_results, dict) or not isinstance(params, dict):
        return None

    username = params.get("username")
    if not isinstance(username, str) or not username.strip():
        return None

    # Locate the membership evidence.
    if "internal_get_membership_status" in tool_results:
        membership_raw = tool_results.get("internal_get_membership_status")
    elif "internal_get_database" in tool_results:
        membership_raw = _membership_from_database(
            tool_results.get("internal_get_database"), username
        )
    else:
        return None

    if membership_raw is _MISSING:
        return None

    # Null membership field -> condition is FALSE (matches negative description).
    if _is_null_membership(membership_raw):
        return False

    membership_dt = _to_datetime(membership_raw)
    if membership_dt is None:
        # Value present but not interpretable as a date: insufficient evidence.
        return None

    interaction_raw = params.get("interaction_time")
    if interaction_raw is None:
        return None
    interaction_dt = _to_datetime(interaction_raw)
    if interaction_dt is None:
        return None

    # Condition: membership date is on or after the interaction time.
    return membership_dt.date() >= interaction_dt.date()
