This document is a compiled Agentic Standard Operating Procedure (ASOP) for the SOPBench `library` domain, produced deterministically from the domain's own dependency tables (`action_required_dependencies`, `action_customizable_dependencies`, `constraint_links`, `constraint_processes`) — no model authored any step or gate.

HOW TO WORK A PROCEDURE. This applies to every step below and is not repeated on them:

1. Work the steps in the order given. That order is not cosmetic: it is an order the domain's action graph permits, and a later step's tool can require an earlier step's tool to have run first.
2. A step marked ESTABLISH performs an action that puts the account into the required state. A step marked VERIFY only reads state — call the tool it names and read the value that comes back. Never assert a condition you have not called a tool for.
3. After the tool result arrives, state one line, in your own words, before going on:
       VERDICT <condition>: SATISFIED - <the value you observed>
   or  VERDICT <condition>: NOT SATISFIED - <the value you observed>
   Cite the actual value the tool returned. A verdict with no value in it is not a verdict.
4. What each condition must RESOLVE to for this particular request — every threshold, every must/must-not — is in the operating rules you were given. Read the value you observed against them. Do not assume a direction.
5. Take the procedure's final action only once every condition above it carries a SATISFIED verdict. If any condition is NOT SATISFIED, do not call the action: say which condition failed and why, and stop.
6. Where a step names a tool followed by "(arguments: ...)", call it with exactly those values, read from the request — the names given are the tool's own argument names, not literals to type in. "(takes no arguments)" means call it with none. This is stated because a tool named in an AND group ("this condition needs X and Y — call them all") does not necessarily take the same arguments as the tool next to it.


## Routing

| The user wants to... | Procedure |
| --- | --- |
| login user; logs in the user to authenticate | Login User |
| show available book; retrieves a list of books available | Show Available Book |
| borrow book; allows a user to borrow a | Borrow Book |
| return book; allows a user to return a | Return Book |
| check return date; retrieves the return date for the | Check Return Date |
| get account balance; retrieves the current balance of the | Get Account Balance |
| credit balance; adds a specified amount to the | Credit Balance |
| pay late fee; deducts the total late fee from | Pay Late Fee |
| update membership; updates the user's restricted access status | Update Membership |
| add book; adds a new book to the | Add Book |
| remove book; removes a book from the library | Remove Book |
| show available rooms; retrieves a dictionary of rooms with | Show Available Rooms |
| reserve room; reserves the specified room for the | Reserve Room |

## Procedure: Login User

This procedure has no preconditions.

1. **Login User — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the login_user tool. Gate: deterministic (tool call: `login_user`)

## Procedure: Show Available Book

One condition must hold before the final action.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool (arguments: username), which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **Show Available Book — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the show_available_book tool. Gate: deterministic (tool call: `show_available_book`)

## Procedure: Borrow Book

ALL 5 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool (arguments: username), which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **internal check book available.** Condition `internal_check_book_available`. VERIFY: call the internal_check_book_available tool (arguments: book_title) and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_check_book_available`)
3. **user book not borrowed.** Condition `user_book_not_borrowed`. VERIFY: this condition needs internal_check_book_exist (arguments: book_title) and internal_get_user_borrowed (arguments: username) — call them all and read the values they return. These read state; they do not change it. Gate: deterministic (tool call: `internal_check_book_exist`)
4. **ANY ONE of: internal is restricted or valid membership.** Conditions `internal_is_restricted, valid_membership` — only one of them need hold. VERIFY: call the internal_is_restricted tool (arguments: book_title) and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_is_restricted`)
5. **within borrow limit.** Condition `within_borrow_limit`. VERIFY: call the internal_get_user_num_borrowed tool (arguments: username) and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_get_user_num_borrowed`)
6. **Borrow Book — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the borrow_book tool. Gate: deterministic (tool call: `borrow_book`)

## Procedure: Return Book

ALL 2 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool (arguments: username), which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **user book borrowed.** Condition `user_book_borrowed`. VERIFY: this condition needs internal_check_book_exist (arguments: book_title) and internal_get_user_borrowed (arguments: username) — call them all and read the values they return. These read state; they do not change it. Gate: deterministic (tool call: `internal_check_book_exist`)
3. **Return Book — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the return_book tool. Gate: deterministic (tool call: `return_book`)

## Procedure: Check Return Date

ALL 2 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool (arguments: username), which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **user book borrowed.** Condition `user_book_borrowed`. VERIFY: this condition needs internal_check_book_exist (arguments: book_title) and internal_get_user_borrowed (arguments: username) — call them all and read the values they return. These read state; they do not change it. Gate: deterministic (tool call: `internal_check_book_exist`)
3. **Check Return Date — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the check_return_date tool. Gate: deterministic (tool call: `check_return_date`)

## Procedure: Get Account Balance

One condition must hold before the final action.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool (arguments: username), which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **Get Account Balance — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the get_account_balance tool. Gate: deterministic (tool call: `get_account_balance`)

## Procedure: Credit Balance

One condition must hold before the final action.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool (arguments: username), which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **Credit Balance — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the credit_balance tool. Gate: deterministic (tool call: `credit_balance`)

## Procedure: Pay Late Fee

ALL 2 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool (arguments: username), which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **sufficient account balance for late fee.** Condition `sufficient_account_balance_for_late_fee`. VERIFY: this condition needs get_account_balance (arguments: username) and internal_calculate_late_fee (arguments: username) — call them all and read the values they return. These read state; they do not change it. Gate: deterministic (tool call: `get_account_balance`)
3. **Pay Late Fee — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the pay_late_fee tool. Gate: deterministic (tool call: `pay_late_fee`)

## Procedure: Update Membership

ALL 2 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool (arguments: username), which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **sufficient account balance for membership.** Condition `sufficient_account_balance_for_membership`. VERIFY: this condition needs get_account_balance (arguments: username) and internal_get_membership_fee (takes no arguments) — call them all and read the values they return. These read state; they do not change it. Gate: deterministic (tool call: `get_account_balance`)
3. **Update Membership — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the update_membership tool. Gate: deterministic (tool call: `update_membership`)

## Procedure: Add Book

ALL 2 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool (arguments: username), which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **internal is admin.** Condition `internal_is_admin`. VERIFY: call the internal_is_admin tool (arguments: username) and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_is_admin`)
3. **Add Book — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the add_book tool. Gate: deterministic (tool call: `add_book`)

## Procedure: Remove Book

ALL 3 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool (arguments: username), which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **internal is admin.** Condition `internal_is_admin`. VERIFY: call the internal_is_admin tool (arguments: username) and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_is_admin`)
3. **database book not borrowed.** Condition `database_book_not_borrowed`. VERIFY: this condition needs internal_check_book_exist (arguments: book_title) and internal_get_user_borrowed (arguments: username) — call them all and read the values they return. These read state; they do not change it. Gate: deterministic (tool call: `internal_check_book_exist`)
4. **Remove Book — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the remove_book tool. Gate: deterministic (tool call: `remove_book`)

## Procedure: Show Available Rooms

One condition must hold before the final action.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool (arguments: username), which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **Show Available Rooms — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the show_available_rooms tool. Gate: deterministic (tool call: `show_available_rooms`)

## Procedure: Reserve Room

ALL 3 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **user login status.** Condition `logged_in_user`. ESTABLISH: call the login_user tool (arguments: username), which puts the account into this state. Gate: deterministic (tool call: `login_user`)
2. **internal all slots available for the room on the date.** Condition `internal_all_slots_available_for_the_room_on_the_date`. VERIFY: call the internal_all_slots_available_for_the_room_on_the_date tool (arguments: room_id, resv_date, slots) and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_all_slots_available_for_the_room_on_the_date`)
3. **ANY ONE of: valid membership or within max reservation slots.** Conditions `valid_membership, within_max_reservation_slots` — only one of them need hold. VERIFY: call the internal_get_membership_status tool (arguments: username) and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_get_membership_status`)
4. **Reserve Room — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the reserve_room tool. Gate: deterministic (tool call: `reserve_room`)
