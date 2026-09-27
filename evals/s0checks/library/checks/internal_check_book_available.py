def check(tool_results: dict, params: dict) -> bool | None:
    """
    Precondition 'internal_check_book_available':
    The book "{book_title}" has a count value of more than 0.

    Evidence: the parsed return value of the internal_check_book_available tool,
    which is True exactly when the book's count is > 0 and False otherwise.

    Returns True / False when the evidence decides the condition,
    or None when the evidence is missing or malformed.
    """
    if not isinstance(params, dict):
        return None

    # The condition is about a specific book; make sure we have its title.
    book_title = params.get("book_title")
    if not isinstance(book_title, str) or not book_title.strip():
        return None

    if not isinstance(tool_results, dict):
        return None

    # The direct evidence for this precondition is the result of the
    # internal_check_book_available tool (True -> count > 0, False -> count < 1).
    availability = tool_results.get("internal_check_book_available")

    if isinstance(availability, bool):
        return availability

    # Tool never called, or its result is malformed / not a boolean:
    # evidence is insufficient -- prefer None over guessing.
    return None
