def check(tool_results: dict, params: dict) -> bool | None:
    """
    Precondition 'database_book_not_borrowed':

    The book's ID, retrieved using the given "book_title" from the
    "book_title_to_id" section of the library database, must NOT appear
    as a key in the "borrowed" dictionaries of ANY user listed in the
    "accounts" section of the database.

    Returns:
        True  - evidence establishes the book's ID is not borrowed by anyone.
        False - evidence establishes the book's ID IS borrowed by at least one user.
        None  - insufficient/malformed evidence to decide.
    """
    # The required parameter must be present and usable.
    book_title = params.get("book_title")
    if not isinstance(book_title, str):
        return None

    # The full database must have been retrieved via internal_get_database.
    db = tool_results.get("internal_get_database")
    if not isinstance(db, dict):
        return None

    # Resolve the book title to its ID via the "book_title_to_id" section.
    title_to_id = db.get("book_title_to_id")
    if not isinstance(title_to_id, dict):
        return None

    book_id = title_to_id.get(book_title)
    if not isinstance(book_id, str):
        # Title not found / ID not resolvable -> cannot decide.
        return None

    # Iterate over every user account and inspect its "borrowed" dictionary.
    accounts = db.get("accounts")
    if not isinstance(accounts, dict):
        return None

    for _username, account in accounts.items():
        if not isinstance(account, dict):
            return None
        borrowed = account.get("borrowed")
        if not isinstance(borrowed, dict):
            # Missing or malformed "borrowed" dict -> evidence incomplete.
            return None
        if book_id in borrowed:
            # The book's ID appears in at least one user's borrowed dict.
            return False

    # The book's ID does not appear in any user's borrowed dictionary.
    return True
