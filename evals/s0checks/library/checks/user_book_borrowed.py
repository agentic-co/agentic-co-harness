def check(tool_results: dict, params: dict) -> bool | None:
    """Deterministic precondition check for 'user_book_borrowed'.

    True  iff the ID of {book_title} (via internal_convert_book_title_to_id,
          or the database's 'book_title_to_id' mapping) exists in the
          'borrowed' collection of user {username} (via
          internal_get_user_borrowed, or the database's
          accounts[username]['borrowed']).
    False iff the evidence definitively shows it is absent.
    None  when the needed evidence is missing or malformed.
    """
    if not isinstance(tool_results, dict) or not isinstance(params, dict):
        return None

    username = params.get("username")
    book_title = params.get("book_title")
    if not isinstance(username, str) or not username.strip():
        return None
    if not isinstance(book_title, str) or not book_title.strip():
        return None

    book_id = _resolve_book_id(tool_results, book_title)
    if not isinstance(book_id, str) or not book_id.strip():
        return None

    borrowed = _resolve_user_borrowed(tool_results, username)
    if not isinstance(borrowed, (list, tuple, set, dict)):
        return None

    return _membership(book_id, borrowed)


def _norm(value):
    return value.strip().casefold() if isinstance(value, str) else None


def _resolve_book_id(tool_results, book_title):
    # Preferred evidence: dedicated title -> id conversion tool.
    converted = tool_results.get("internal_convert_book_title_to_id")
    if isinstance(converted, str) and converted.strip():
        return converted

    # Fallback evidence: full database's 'book_title_to_id' section.
    db = tool_results.get("internal_get_database")
    if isinstance(db, dict):
        mapping = db.get("book_title_to_id")
        if isinstance(mapping, dict):
            exact = mapping.get(book_title)
            if isinstance(exact, str) and exact.strip():
                return exact
            norm_title = _norm(book_title)
            for key, value in mapping.items():
                if _norm(key) == norm_title and isinstance(value, str) and value.strip():
                    return value
    return None


def _resolve_user_borrowed(tool_results, username):
    # Preferred evidence: dedicated tool for this user's borrowed ids.
    user_borrowed = tool_results.get("internal_get_user_borrowed")
    if isinstance(user_borrowed, (list, tuple, set, dict)):
        return user_borrowed

    # Fallback evidence: full database's accounts section.
    db = tool_results.get("internal_get_database")
    if isinstance(db, dict):
        accounts = db.get("accounts")
        if isinstance(accounts, dict):
            profile = accounts.get(username)
            if isinstance(profile, dict):
                borrowed = profile.get("borrowed")
                if isinstance(borrowed, (list, tuple, set, dict)):
                    return borrowed
    return None


def _membership(book_id, borrowed):
    if isinstance(borrowed, dict):
        if book_id in borrowed:
            return True
        norm_id = _norm(book_id)
        for key in borrowed.keys():
            if _norm(key) == norm_id:
                return True
        return False
    if isinstance(borrowed, (list, tuple, set)):
        if book_id in borrowed:
            return True
        norm_id = _norm(book_id)
        for item in borrowed:
            if _norm(item) == norm_id:
                return True
        return False
    return None
