This document is a compiled Agentic Standard Operating Procedure (ASOP) for the SOPBench `hotel` domain, produced deterministically from the domain's own dependency tables (`action_required_dependencies`, `action_customizable_dependencies`, `constraint_links`, `constraint_processes`) — no model authored any step or gate.

HOW TO WORK A PROCEDURE. This applies to every step below and is not repeated on them:

1. Work the steps in the order given. That order is not cosmetic: it is an order the domain's action graph permits, and a later step's tool can require an earlier step's tool to have run first.
2. A step marked ESTABLISH performs an action that puts the account into the required state. A step marked VERIFY only reads state — call the tool it names and read the value that comes back. Never assert a condition you have not called a tool for.
3. After the tool result arrives, state one line, in your own words, before going on:
       VERDICT <condition>: SATISFIED - <the value you observed>
   or  VERDICT <condition>: NOT SATISFIED - <the value you observed>
   Cite the actual value the tool returned. A verdict with no value in it is not a verdict.
4. What each condition must RESOLVE to for this particular request — every threshold, every must/must-not — is in the operating rules you were given. Read the value you observed against them. Do not assume a direction.
5. Take the procedure's final action only once every condition above it carries a SATISFIED verdict. If any condition is NOT SATISFIED, do not call the action: say which condition failed and why, and stop.


## Routing

| The user wants to... | Procedure |
| --- | --- |
| show available rooms; displays available rooms across all room | Show Available Rooms |
| show room change options; lists valid reasons a guest can | Show Room Change Options |
| book room; books a room for the guest | Book Room |
| find booking info; finds the booking information for the | Find Booking Info |
| cancel reservation; cancels a confirmed reservation for the | Cancel Reservation |
| modify reservation; modifies the guest's existing reservation to | Modify Reservation |
| process guest checkin; processes the check-in of a guest | Process Guest Checkin |
| process guest checkout; processes the checkout of a guest | Process Guest Checkout |
| request room change; processes a room change request by | Request Room Change |
| place room service order; places a new room service order | Place Room Service Order |
| register loyalty member; registers the specified guest into the | Register Loyalty Member |

## Procedure: Show Available Rooms

This procedure has no preconditions.

1. **Show Available Rooms — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the show_available_rooms tool. Gate: deterministic (tool call: `show_available_rooms`)

## Procedure: Show Room Change Options

This procedure has no preconditions.

1. **Show Room Change Options — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the show_room_change_options tool. Gate: deterministic (tool call: `show_room_change_options`)

## Procedure: Book Room

ALL 5 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **room type available for dates.** Condition `room_type_available_for_dates`. VERIFY: call the show_available_rooms tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `show_available_rooms`)
2. **sufficient amount for booking.** Condition `sufficient_amount_for_booking`. VERIFY: call the show_available_rooms tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `show_available_rooms`)
3. **has overlapping booking for booking.** Condition `has_overlapping_booking_for_booking`. VERIFY: call the internal_get_booking_details tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_get_booking_details`)
4. **is booking date within lead time range.** Condition `is_booking_date_within_lead_time_range`. VERIFY: call the internal_get_interaction_time tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_get_interaction_time`)
5. **ANY ONE of: has exceeded maximum stays or is gold or higher member.** Conditions `has_exceeded_maximum_stays, is_gold_or_higher_member` — only one of them need hold. No tool in this domain can check it; read it off the rules and the request. Gate: judged
6. **Book Room — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the book_room tool. Gate: deterministic (tool call: `book_room`)

## Procedure: Find Booking Info

This procedure has no preconditions.

1. **Find Booking Info — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the find_booking_info tool. Gate: deterministic (tool call: `find_booking_info`)

## Procedure: Cancel Reservation

ALL 2 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **has confirmed reservation.** Condition `has_confirmed_reservation`. VERIFY: call the internal_get_booking_details tool and read the value it returns. This reads state; it does not change it. It can alternatively be established from find_booking_info. Gate: deterministic (tool call: `internal_get_booking_details`)
2. **before modification deadline.** Condition `before_modification_deadline`. VERIFY: call the internal_get_interaction_time tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_get_interaction_time`)
3. **Cancel Reservation — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the cancel_reservation tool. Gate: deterministic (tool call: `cancel_reservation`)

## Procedure: Modify Reservation

ALL 6 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **room type available for dates.** Condition `room_type_available_for_dates`. VERIFY: call the show_available_rooms tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `show_available_rooms`)
2. **sufficient amount for reservation modification.** Condition `sufficient_amount_for_reservation_modification`. VERIFY: this condition needs internal_get_booking_details and show_available_rooms — call them all and read the values they return. These read state; they do not change it. It can alternatively be established from find_booking_info. Gate: deterministic (tool call: `internal_get_booking_details`)
3. **has overlapping booking for modification.** Condition `has_overlapping_booking_for_modification`. VERIFY: this condition needs internal_get_booking_details and find_booking_info — call them all and read the values they return. These read state; they do not change it. Gate: deterministic (tool call: `internal_get_booking_details`)
4. **is booking date within lead time range.** Condition `is_booking_date_within_lead_time_range`. VERIFY: call the internal_get_interaction_time tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_get_interaction_time`)
5. **before modification deadline.** Condition `before_modification_deadline`. VERIFY: call the internal_get_interaction_time tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_get_interaction_time`)
6. **ANY ONE of: has exceeded maximum stays or is gold or higher member.** Conditions `has_exceeded_maximum_stays, is_gold_or_higher_member` — only one of them need hold. No tool in this domain can check it; read it off the rules and the request. Gate: judged
7. **Modify Reservation — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the modify_reservation tool. Gate: deterministic (tool call: `modify_reservation`)

## Procedure: Process Guest Checkin

ALL 3 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **has confirmed reservation.** Condition `has_confirmed_reservation`. VERIFY: call the internal_get_booking_details tool and read the value it returns. This reads state; it does not change it. It can alternatively be established from find_booking_info. Gate: deterministic (tool call: `internal_get_booking_details`)
2. **valid identification.** Condition `valid_identification`. VERIFY: call the internal_get_interaction_time tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_get_interaction_time`)
3. **after check in time.** Condition `after_check_in_time`. VERIFY: call the internal_get_interaction_time tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_get_interaction_time`)
4. **Process Guest Checkin — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the process_guest_checkin tool. Gate: deterministic (tool call: `process_guest_checkin`)

## Procedure: Process Guest Checkout

ALL 3 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **guest already checked in.** Condition `guest_already_checked_in`. VERIFY: this condition needs internal_get_booking_details and internal_get_room_checkin_details — call them all and read the values they return. These read state; they do not change it. Gate: deterministic (tool call: `internal_get_booking_details`)
2. **room key returned.** Condition `room_key_returned`. No tool in this domain can check it; read it off the rules and the request. Gate: judged
3. **before check out time.** Condition `before_check_out_time`. VERIFY: call the internal_get_interaction_time tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_get_interaction_time`)
4. **Process Guest Checkout — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the process_guest_checkout tool. Gate: deterministic (tool call: `process_guest_checkout`)

## Procedure: Request Room Change

ALL 3 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **sufficient amount for room change fee.** Condition `sufficient_amount_for_room_change_fee`. VERIFY: this condition needs internal_get_interaction_time and internal_get_booking_details — call them all and read the values they return. These read state; they do not change it. Gate: deterministic (tool call: `internal_get_interaction_time`)
2. **internal valid room change reason.** Condition `internal_valid_room_change_reason`. VERIFY: call the internal_valid_room_change_reason tool and read the value it returns. This reads state; it does not change it. It can alternatively be established from show_room_change_options. Gate: deterministic (tool call: `internal_valid_room_change_reason`)
3. **within max room changes.** Condition `within_max_room_changes`. VERIFY: call the internal_get_booking_details tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_get_booking_details`)
4. **Request Room Change — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the request_room_change tool. Gate: deterministic (tool call: `request_room_change`)

## Procedure: Place Room Service Order

ALL 4 conditions below must hold before the final action. Work them in the order given — it is the order the domain's own action graph allows, and a later step's tool may depend on an earlier step's having run.

1. **guest already checked in.** Condition `guest_already_checked_in`. VERIFY: this condition needs internal_get_booking_details and internal_get_room_checkin_details — call them all and read the values they return. These read state; they do not change it. Gate: deterministic (tool call: `internal_get_booking_details`)
2. **sufficient payment for room service.** Condition `sufficient_payment_for_room_service`. VERIFY: call the internal_compute_room_service_order_fee tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_compute_room_service_order_fee`)
3. **within room service order daily limit.** Condition `within_room_service_order_daily_limit`. VERIFY: this condition needs internal_get_interaction_time and internal_get_booking_details and internal_get_room_assignment — call them all and read the values they return. These read state; they do not change it. Gate: deterministic (tool call: `internal_get_interaction_time`)
4. **within room service hours.** Condition `within_room_service_hours`. VERIFY: call the internal_get_interaction_time tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_get_interaction_time`)
5. **Place Room Service Order — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the place_room_service_order tool. Gate: deterministic (tool call: `place_room_service_order`)

## Procedure: Register Loyalty Member

One condition must hold before the final action.

1. **internal is loyalty member.** Condition `internal_is_loyalty_member`. VERIFY: call the internal_is_loyalty_member tool and read the value it returns. This reads state; it does not change it. Gate: deterministic (tool call: `internal_is_loyalty_member`)
2. **Register Loyalty Member — the requested action.** ACT: every condition above must carry a SATISFIED verdict first. Then call the register_loyalty_member tool. Gate: deterministic (tool call: `register_loyalty_member`)
