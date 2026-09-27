def _resolve_book_id(tool_results, book_title):
    """Resolve the book's ID using the book_title from the 'book_title_to_id'
    section, via the dedicated converter tool result or the full database."""
    converted = tool_results.get("internal_convert_book_title_to_id")
    if isinstance(converted, str) and converted:
        return converted

    db = tool_results.get("internal_get_database")
    if isinstance(db, dict):
        title_to_id = db.get("book_title_to_id")
        if isinstance(title_to_id, dict) and book_title in title_to_id:
            mapped = title_to_id.get(book_title)
            if isinstance(mapped, str) and mapped:
                return mapped
    return None


def _resolve_borrowed_list(tool_results, username):
    """Resolve the user's 'borrowed' list of book IDs, via the dedicated
    lookup tool result or the user's profile in the full database."""
    user_borrowed = tool_results.get("internal_get_user_borrowed")
    if isinstance(user_borrowed, list):
        return user_borrowed

    db = tool_results.get("internal_get_database")
    if not isinstance(db, dict):
        return None

    sections = []
    users_section = db.get("users")
    if isinstance(users_section, dict):
        sections.append(users_section)
    for value in db.values():
        if (
            isinstance(value, dict)
            and username in value
            and not any(value is s for s in sections)
        ):
            sections.append(value)

    for section in sections:
        profile = section.get(username)
        if isinstance(profile, dict):
            borrowed = profile.get("borrowed")
            if isinstance(borrowed, list):
                return borrowed
    return None


def check(tool_results: dict, params: dict):
    """Deterministic precondition checker for 'user_book_not_borrowed'.

    True  -> the book's ID is NOT in the user's 'borrowed' list.
    False -> the book's ID IS in the user's 'borrowed' list.
    None  -> insufficient evidence to decide.
    """
    if not isinstance(tool_results, dict) or not isinstance(params, dict):
        return None

    username = params.get("username")
    book_title = params.get("book_title")
    if not isinstance(username, str) or not username:
        return None
    if not isinstance(book_title, str) or not book_title:
        return None

    book_id = _resolve_book_id(tool_results, book_title)
    if not book_id:
        return None

    borrowed = _resolve_borrowed_list(tool_results, username)
    if borrowed is None:
        return None

    try:
        is_borrowed = any(entry == book_id for entry in borrowed)
    except Exception:
        return None

    return not is_borrowed
