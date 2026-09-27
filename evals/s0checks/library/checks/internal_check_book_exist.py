def _book_id_in_books(book_id, books):
    """Return True/False if membership of book_id in the 'books' collection can
    be determined from its shape, or None if the collection is malformed."""
    if book_id is None or isinstance(book_id, bool):
        return None

    if isinstance(books, dict):
        if book_id in books:
            return True
        # Lenient comparison for stringified ids (e.g. 5 vs "5").
        return any(str(key) == str(book_id) for key in books.keys())

    if isinstance(books, list):
        if book_id in books:
            return True
        for entry in books:
            if isinstance(entry, dict):
                for key in ("book_id", "id", "bookId"):
                    if key in entry and (entry[key] == book_id or str(entry[key]) == str(book_id)):
                        return True
            elif isinstance(entry, (str, int)) and str(entry) == str(book_id):
                return True
        return False

    return None


def check(tool_results: dict, params: dict) -> bool | None:
    """Precondition 'internal_check_book_exist':
    True iff params['book_title'] exists in the database's 'book_title_to_id'
    mapping AND the resulting book id exists in the database's 'books' section.
    Returns None when available evidence is insufficient to decide."""
    if not isinstance(tool_results, dict) or not isinstance(params, dict):
        return None

    title = params.get("book_title")
    if not isinstance(title, str):
        return None

    # 1) Direct evidence: the dedicated existence-check tool was already run.
    direct = tool_results.get("internal_check_book_exist")
    if isinstance(direct, bool):
        return direct

    # 2) Raw database evidence.
    db = tool_results.get("internal_get_database")
    mapping = None
    books = None
    if isinstance(db, dict):
        m = db.get("book_title_to_id")
        if isinstance(m, dict):
            mapping = m
        b = db.get("books")
        if isinstance(b, (dict, list)):
            books = b

    if mapping is not None:
        if title not in mapping:
            # Title absent from 'book_title_to_id' -> condition is false.
            return False
        book_id = mapping[title]
        if books is None:
            # Only the title->id half is established; id-in-books unverifiable.
            return None
        return _book_id_in_books(book_id, books)

    # 3) Fallback: a successful title->id conversion implies the title exists
    #    in 'book_title_to_id'; then verify the id exists in 'books'.
    conv = tool_results.get("internal_convert_book_title_to_id")
    if (
        books is not None
        and isinstance(conv, (str, int))
        and not isinstance(conv, bool)
        and str(conv) != ""
    ):
        return _book_id_in_books(conv, books)

    return None
