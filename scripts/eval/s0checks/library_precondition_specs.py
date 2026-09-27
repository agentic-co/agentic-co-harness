"""Per-precondition prompt inputs for the S0-checks generator, library domain
(transfer test -- see hotel's precondition_specs.py for the anti-leakage
rationale, which applies identically here).

The 17 names below are exactly the constraint leaves that actually appear in
`data/library_tasks.json`'s own constraint trees (computed with
`sopbench_judge_gate.task_leeaves` over every task, stripping "not "), the
same non-leaky derivation used for hotel's 15 -- not chosen by hand and not
read from any judge log (library has no jev-style value-gate log to read).

All SYNTHETIC_* values are invented (fictitious usernames/book ids/room ids),
never copied from `env/domains/library/library.py`'s `default_data`.

One domain quirk worth recording: `valid_membership`'s ground truth compares
a membership date to the current interaction date, but library deliberately
does NOT expose `internal_get_interaction_date` as an agent tool (see
`scripts/eval/sopbench_extract.py`'s `environment_verified_nodes` docstring --
it is withheld on purpose, one of exactly two such nodes in the whole
benchmark). A generated checker that honestly abstains without that evidence
will show ~0 coverage on `valid_membership`; that is the domain's design, not
a generation failure, and is called out again in PLAN.md.
"""

PRECONDITIONS = [
    "logged_in_user",
    "user_book_borrowed",
    "user_book_not_borrowed",
    "database_book_not_borrowed",
    "sufficient_account_balance_for_late_fee",
    "sufficient_account_balance_for_membership",
    "valid_membership",
    "within_borrow_limit",
    "within_max_reservation_slots",
    "internal_check_username_exist",
    "internal_check_book_exist",
    "internal_check_book_available",
    "internal_is_restricted",
    "internal_is_admin",
    "internal_check_room_exist",
    "internal_check_date_available_for_the_room",
    "internal_all_slots_available_for_the_room_on_the_date",
]

HINT_TOOLS = {
    "logged_in_user": ["login_user"],
    "user_book_borrowed": ["internal_get_user_borrowed"],
    "user_book_not_borrowed": ["internal_get_user_borrowed"],
    "database_book_not_borrowed": ["internal_get_database"],
    "sufficient_account_balance_for_late_fee": ["get_account_balance", "internal_calculate_late_fee"],
    "sufficient_account_balance_for_membership": ["get_account_balance", "internal_get_membership_fee"],
    "valid_membership": ["internal_get_membership_status"],
    "within_borrow_limit": ["internal_get_user_num_borrowed"],
    "within_max_reservation_slots": ["internal_get_num_reserved_slots"],
    "internal_check_username_exist": ["internal_check_username_exist"],
    "internal_check_book_exist": ["internal_check_book_exist"],
    "internal_check_book_available": ["internal_check_book_available"],
    "internal_is_restricted": ["internal_is_restricted"],
    "internal_is_admin": ["internal_is_admin"],
    "internal_check_room_exist": ["internal_check_room_exist"],
    "internal_check_date_available_for_the_room": ["internal_check_date_available_for_the_room"],
    "internal_all_slots_available_for_the_room_on_the_date": ["internal_all_slots_available_for_the_room_on_the_date"],
}

# Invented (NOT from library.py's default_data / any task / any log).
SYNTHETIC_TOOL_OUTPUTS = {
    "login_user": True,
    "internal_get_user_borrowed": ["QX10ZZ9"],
    "internal_get_database": {
        "accounts": {
            "user_alpha": {"borrowed": {"QX10ZZ9": "2031-02-01"}, "admin": False, "balance": 12,
                           "membership": "2031-03-01", "late_book_count": 0, "room_reservation": {}},
            "user_beta": {"borrowed": {}, "admin": True, "balance": 5,
                          "membership": None, "late_book_count": 1, "room_reservation": {}},
        },
        "books": {"QX10ZZ9": {"count": 2, "restricted": False}},
        "book_title_to_id": {"Practical Zoology": "QX10ZZ9"},
    },
    "get_account_balance": 42,
    "internal_calculate_late_fee": 6,
    "internal_get_membership_fee": 15,
    "internal_get_membership_status": "2031-03-01",
    "internal_get_user_num_borrowed": 2,
    "internal_get_num_reserved_slots": 3,
    "internal_check_username_exist": True,
    "internal_check_book_exist": True,
    "internal_check_book_available": True,
    "internal_is_restricted": False,
    "internal_is_admin": False,
    "internal_check_room_exist": True,
    "internal_check_date_available_for_the_room": True,
    "internal_all_slots_available_for_the_room_on_the_date": True,
}

SYNTHETIC_PARAMS = {
    "logged_in_user": {"username": "user_alpha"},
    "user_book_borrowed": {"username": "user_alpha", "book_title": "Practical Zoology"},
    "user_book_not_borrowed": {"username": "user_alpha", "book_title": "Practical Zoology"},
    "database_book_not_borrowed": {"book_title": "Practical Zoology"},
    "sufficient_account_balance_for_late_fee": {"username": "user_alpha"},
    "sufficient_account_balance_for_membership": {"username": "user_alpha"},
    "valid_membership": {"username": "user_alpha"},
    "within_borrow_limit": {"username": "user_alpha", "borrow_limit": 5},
    "within_max_reservation_slots": {"username": "user_alpha", "slots": ["13:00", "14:00"], "max_reservation_slots": 4},
    "internal_check_username_exist": {"username": "user_alpha"},
    "internal_check_book_exist": {"book_title": "Practical Zoology"},
    "internal_check_book_available": {"book_title": "Practical Zoology"},
    "internal_is_restricted": {"book_title": "Practical Zoology"},
    "internal_is_admin": {"username": "user_alpha"},
    "internal_check_room_exist": {"room_id": "LB099"},
    "internal_check_date_available_for_the_room": {"room_id": "LB099", "resv_date": "2031-02-10"},
    "internal_all_slots_available_for_the_room_on_the_date": {"room_id": "LB099", "resv_date": "2031-02-10", "slots": ["13:00", "14:00"]},
}
