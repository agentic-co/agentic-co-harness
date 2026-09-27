from __future__ import annotations

from datetime import datetime, date


def _parse_date_str(value):
    """Parse a 'YYYY-MM-DD' string into a date. Return None if invalid."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _parse_reference_date(timestamp_value):
    """Parse an interaction timestamp into a date. Return None if invalid."""
    if not isinstance(timestamp_value, str):
        return None
    txt = timestamp_value.strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(txt, fmt).date()
        except (ValueError, TypeError):
            continue
    try:
        return datetime.fromisoformat(txt).date()
    except (ValueError, TypeError):
        return None


def check(tool_results: dict, params: dict) -> bool | None:
    """
    Precondition 'valid_identification':
      The params['identification'] must include a 'type' that matches one of
      params['valid_document_types'] and a valid 'birthday' indicating the
      guest is at least params['min_age'] years old (age measured against the
      current system interaction time from the internal_get_interaction_time
      tool result).

    Returns True if satisfied, False if not satisfied, None if the evidence
    available is insufficient to decide.
    """
    if not isinstance(tool_results, dict) or not isinstance(params, dict):
        return None

    identification = params.get("identification")
    valid_document_types = params.get("valid_document_types")
    min_age = params.get("min_age")

    # Missing required param values -> cannot decide.
    if identification is None or valid_document_types is None or min_age is None:
        return None

    # Malformed param values -> cannot decide.
    if not isinstance(identification, dict):
        return None
    if not isinstance(valid_document_types, (list, tuple, set, frozenset)):
        return None
    if isinstance(min_age, bool) or not isinstance(min_age, (int, float)):
        return None

    # --- Document type check ---
    # The identification must include a 'type' that matches one of the valid types.
    doc_type = identification.get("type")
    if not isinstance(doc_type, str) or doc_type not in valid_document_types:
        return False

    # --- Birthday check ---
    # The identification must include a valid 'birthday' (YYYY-MM-DD).
    birthday = _parse_date_str(identification.get("birthday"))
    if birthday is None:
        return False

    # Age is measured against the current system interaction time.
    if "internal_get_interaction_time" not in tool_results:
        return None
    reference_date = _parse_reference_date(
        tool_results.get("internal_get_interaction_time")
    )
    if reference_date is None:
        return None

    # A birthday in the future means the guest is not yet born and cannot be
    # at least min_age years old.
    if birthday > reference_date:
        return False

    age = reference_date.year - birthday.year - (
        (reference_date.month, reference_date.day) < (birthday.month, birthday.day)
    )
    return age >= min_age
