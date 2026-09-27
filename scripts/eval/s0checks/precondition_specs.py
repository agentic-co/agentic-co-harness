"""Per-precondition prompt inputs for the S0-checks generator (hotel domain).

ANTI-LEAKAGE, stated once here because every value in this file was chosen to
respect it: everything below is either (a) copied verbatim from SOPBench's own
domain schema/description dicts (`evals/s0checks/hotel/domain_schema.json`,
itself dumped straight from `env.domains.hotel.hotel_assistant`), or (b) a
SYNTHETIC example this file's author invented from the shape of a tool's JSON
schema -- fictitious guests, room numbers, dates and reason strings that do
not appear in SOPBench's `default_data` and are not drawn from any task or
from `jev.verdicts.jsonl`. No logged row, no `truth_of` output, and no real
`default_data` value is used anywhere in generation.

`hint_tools`: at most 2 tool names per precondition, used only to decide which
2 synthetic tool-output examples to show the generator for THAT precondition
(the model still sees the full domain tool catalog and must decide for
itself which tools are relevant -- these hints only bound how many invented
examples we bother to write, per the eval's "at most 2 synthetic tool
outputs" cap).
"""

PRECONDITIONS = [
    "room_type_available_for_dates",
    "sufficient_amount_for_booking",
    "has_overlapping_booking_for_booking",
    "has_overlapping_booking_for_modification",
    "has_confirmed_reservation",
    "sufficient_amount_for_reservation_modification",
    "before_modification_deadline",
    "is_booking_date_within_lead_time_range",
    "after_check_in_time",
    "before_check_out_time",
    "guest_already_checked_in",
    "valid_identification",
    "internal_valid_room_change_reason",
    "internal_is_loyalty_member",
    "sufficient_payment_for_room_service",
]

HINT_TOOLS = {
    "room_type_available_for_dates": ["show_available_rooms"],
    "sufficient_amount_for_booking": ["show_available_rooms"],
    "has_overlapping_booking_for_booking": ["internal_get_booking_details"],
    "has_overlapping_booking_for_modification": ["internal_get_booking_details"],
    "has_confirmed_reservation": ["internal_get_booking_details"],
    "sufficient_amount_for_reservation_modification": ["internal_get_booking_details", "show_available_rooms"],
    "before_modification_deadline": ["internal_get_interaction_time"],
    "is_booking_date_within_lead_time_range": ["internal_get_interaction_time"],
    "after_check_in_time": ["internal_get_interaction_time"],
    "before_check_out_time": ["internal_get_interaction_time"],
    "guest_already_checked_in": ["internal_get_booking_details", "internal_get_room_checkin_details"],
    "valid_identification": ["internal_get_interaction_time"],
    "internal_valid_room_change_reason": ["show_room_change_options"],
    "internal_is_loyalty_member": ["internal_is_loyalty_member"],
    "sufficient_payment_for_room_service": ["internal_compute_room_service_order_fee", "internal_get_loyalty_member_info"],
}

# Invented (NOT from default_data / any task / any log). Values chosen so they
# cannot be confused with SOPBench's real defaults (different room ids, guest
# name, dates, reason vocabulary, member id).
SYNTHETIC_TOOL_OUTPUTS = {
    "show_available_rooms": {
        "single": {"availability": {"701": ["2031-06-10", "2031-06-11"]}, "price_per_night": 95},
        "deluxe": {"availability": {"820": ["2031-06-10", "2031-06-11", "2031-06-12"]}, "price_per_night": 150},
    },
    "internal_get_booking_details": {
        "BKZ1": {
            "guest": "Q. Sample", "room_type": "single",
            "check_in_date": "2031-06-10", "check_out_date": "2031-06-12",
            "booking_time": "2031-05-01T10:00:00", "status": "confirmed",
            "loyalty_points_to_add": 0, "room_change": 0, "room_service": {},
        },
    },
    "internal_get_interaction_time": "2031-06-09T14:00:00",
    "internal_get_room_checkin_details": {
        "701": {"booking_id": "BKZ1", "check_in_time": "2031-06-10T15:20:00", "identity_document": "passport"},
    },
    "internal_get_loyalty_member_info": {
        "HTLZZZZ": {"name": "Q. Sample", "loyalty_points": 40, "tier": "silver"},
    },
    "internal_compute_room_service_order_fee": 37,
    "show_room_change_options": ["upgrade_request", "facility_issue", "other"],
    "internal_is_loyalty_member": True,
}

# Small, invented params examples per precondition (not a "tool output", so
# not counted against the 2-example cap; included only to show the *shape* of
# params -- flat dict, keys named exactly as in the description's {braces}).
SYNTHETIC_PARAMS = {
    "room_type_available_for_dates": {"room_type": "single", "check_in_date": "2031-06-10", "check_out_date": "2031-06-12"},
    "sufficient_amount_for_booking": {"room_type": "single", "check_in_date": "2031-06-10", "check_out_date": "2031-06-12", "amount": 190},
    "has_overlapping_booking_for_booking": {"guest_name": "Q. Sample", "check_in_date": "2031-06-11", "check_out_date": "2031-06-13"},
    "has_overlapping_booking_for_modification": {"guest_name": "Q. Sample", "old_check_in_date": "2031-06-10", "old_check_out_date": "2031-06-12", "check_in_date": "2031-06-11", "check_out_date": "2031-06-13"},
    "has_confirmed_reservation": {"guest_name": "Q. Sample", "check_in_date": "2031-06-10", "check_out_date": "2031-06-12"},
    "sufficient_amount_for_reservation_modification": {"guest_name": "Q. Sample", "old_check_in_date": "2031-06-10", "old_check_out_date": "2031-06-12", "check_in_date": "2031-06-10", "check_out_date": "2031-06-14", "room_type": "deluxe", "amount": 300},
    "before_modification_deadline": {"check_in_date": "2031-06-10", "check_in_time": "15:00", "modification_deadline_hours": 48},
    "is_booking_date_within_lead_time_range": {"check_in_date": "2031-06-10", "min_booking_lead_time_days": 1, "max_booking_lead_time_days": 30},
    "after_check_in_time": {"check_in_time": "15:00"},
    "before_check_out_time": {"check_out_time": "11:00"},
    "guest_already_checked_in": {"guest_name": "Q. Sample"},
    "valid_identification": {"identification": {"type": "passport", "birthday": "2005-01-01"}, "valid_document_types": ["driver_license", "passport", "state_id", "military_id"], "min_age": 18},
    "internal_valid_room_change_reason": {"reason": "facility_issue"},
    "internal_is_loyalty_member": {"guest_name": "Q. Sample"},
    "sufficient_payment_for_room_service": {"guest_name": "Q. Sample", "order_type": "dining", "order_items": [{"name": "coffee", "quantity": 2}], "payment_method": "loyalty_points", "amount": None},
}
