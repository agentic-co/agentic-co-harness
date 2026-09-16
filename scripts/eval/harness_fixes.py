#!/usr/bin/env python3
"""Two measured scoring faults in the tau2 airline harness, and what we do about each.

Both were found on 2026-09-15 while diffing arm B against arm D on task 23. Both
inflate the apparent penalty on a gated/ASOP arm, so leaving them in place biases
the experiment toward its own hypothesis. They are handled DIFFERENTLY and the
difference is the point:

  FAULT 1  payment_history list order  ->  FIXED, by canonicalising the hash input.
  FAULT 2  user grants consent and stops in the same turn  ->  NOT fixed. Detected,
           and those cells are reported as unscoreable rather than silently counted.

Why not fix both. Fault 1 is a normalisation: a reservation paid with the same
multiset of instruments IS the same reservation, so making the hash agree with that
cannot turn a wrong answer into a right one. Fault 2 would require changing when
conversations END, which changes the benchmark's dynamics for every arm and every
task — a much larger intervention than the 2 affected cells justify. Refusing to
score what we cannot score is this project's existing doctrine (see
`t1_label.py score`, which refuses on a single-class set); this follows it.

Applied by the runners, not by editing the tau2 checkout: the checkout lives in a
scratchpad, is not ours, and is not version controlled. A patch that exists only
there is a result nobody can reproduce.
"""

from __future__ import annotations

import re
from typing import Any

# The user simulator is instructed to end a conversation with this marker. It
# sometimes emits it in the same turn as an instruction, and `UserSimulator.is_stop`
# fires on the marker appearing ANYWHERE in the content, so the conversation ends
# before the agent can act on what it was just told.
STOP_MARKER = "###STOP###"

# Deliberately narrow. Matches consent to proceed, not a polite decline — "I can't
# proceed with the upgrade" and "there is nothing more we can do" are the failure
# mode of a loose pattern here, and both appear in the real logs.
_CONSENT = re.compile(
    r"(?:^|[.!?]\s|\n)\s*(?:yes|yep|sure|ok(?:ay)?)\b[^.!?]{0,80}"
    r"|(?:please\s+)?(?:go ahead|proceed|do it|book it|cancel it|confirm it)\b",
    re.I,
)
_REFUSAL = re.compile(
    r"\b(?:can(?:no|')t|cannot|won't|unable to|don't want|do not want|too much|"
    r"nothing more|no other options)\b",
    re.I,
)


def canonicalise_payment_history(domain: str = "airline") -> str:
    """FAULT 1 — make the DB hash agree that payment order is not payment identity.

    tau2 hashes database state with `get_dict_hash` = `json.dumps(obj, sort_keys=True)`,
    which sorts dict KEYS but preserves LIST order. `Reservation.payment_history` is a
    `List[Payment]`, so booking with [certificate, gift_card, credit_card] hashes
    differently from [credit_card, certificate, gift_card] even though both describe
    one reservation paid the same way by the same instruments.

    Measured consequence: on task 23 trial 0 arm B and arm D made the same ten tool
    calls with semantically identical payloads, differing only in that order. Arm B
    scored 1.0, arm D scored 0.0.

    ONLY `payment_history` is canonicalised. `flights` is left alone — its order is
    the itinerary, and sorting it would make genuinely different journeys hash equal,
    turning a scoring fault into a scoring hole. This narrows what counts as a match;
    it never widens it.

    Returns a description of what was patched, for the run log.
    """
    # The fault was measured in airline, where `Reservation.payment_history` is a
    # `List[Payment]`. Another domain may have the same shape under another name, or
    # may not have it at all — silently patching nothing while printing that a fix was
    # applied is how a run acquires a reassurance it has not earned. Say which it is.
    if domain != "airline":
        return (
            f"payment_history canonicalisation: NOT APPLIED — measured in airline, "
            f"and this run is {domain!r}. Check whether {domain} has an order-sensitive "
            f"list in its DB hash before trusting a close result from it."
        )

    from tau2.domains.airline.tools import AirlineTools
    from tau2.utils.utils import get_dict_hash

    if getattr(AirlineTools, "_payment_order_canonicalised", False):
        return "payment_history canonicalisation: already applied"

    def _sorted_payments(obj: Any) -> Any:
        if isinstance(obj, dict):
            out = {}
            for key, value in obj.items():
                if key == "payment_history" and isinstance(value, list):
                    out[key] = sorted(
                        (_sorted_payments(v) for v in value),
                        key=lambda p: (str(p.get("payment_id", "")), p.get("amount", 0))
                        if isinstance(p, dict)
                        else str(p),
                    )
                else:
                    out[key] = _sorted_payments(value)
            return out
        if isinstance(obj, list):
            return [_sorted_payments(v) for v in obj]
        return obj

    def get_db_hash(self) -> str:
        return get_dict_hash(_sorted_payments(self.db.model_dump()))

    AirlineTools.get_db_hash = get_db_hash
    AirlineTools._payment_order_canonicalised = True
    return "payment_history canonicalised before hashing (flights order untouched)"


def defer_consent_stop() -> str:
    """FAULT 2, now FIXED rather than merely detected — see the module note.

    Why the change of mind. On airline this cost 2 cells in 104 and a real fix
    meant changing when conversations end for every arm and task — not worth it.
    On retail it costs **13 of 36 cells**, because every mutating action there
    requires a confirmation under G3, so there are far more confirmation turns for
    the simulator to end on. At 36% it is not a rounding error, it is most of our
    statistical power, and excluding those cells shrinks the comparison basis that
    every conclusion rests on.

    The fix: when a user's stop message also GRANTS CONSENT ("Yes, please proceed
    with the cancellation. ###STOP###"), the conversation does not end on that
    message. The agent gets one more turn to do the thing it was just told to do.

    Bounded two ways, because a stop signal that can be ignored is a hang waiting
    to happen: each message defers at most once (keyed by object identity), and
    `max_steps` remains the hard backstop. In practice the simulator's next turn
    is a bare sign-off carrying no consent, so the conversation ends there.

    This CHANGES CONVERSATION DYNAMICS. Numbers from before it are not comparable
    with numbers after it, for any arm — a baseline has to be re-run alongside any
    treatment, or the treatment is being compared to a different benchmark.
    """
    from tau2.user.user_simulator import UserSimulator

    if getattr(UserSimulator, "_consent_stop_deferred", False):
        return "consent-stop deferral: already applied"

    original = UserSimulator.is_stop.__func__
    deferred: set[int] = set()

    def is_stop(cls, message):  # type: ignore[no-untyped-def]
        stopping = original(cls, message)
        if not stopping:
            return False
        content = getattr(message, "content", None) or ""
        said = content.replace(STOP_MARKER, "").strip()
        if _REFUSAL.search(said) or not _CONSENT.search(said):
            return True
        if id(message) in deferred:
            return True
        deferred.add(id(message))
        return False

    UserSimulator.is_stop = classmethod(is_stop)
    UserSimulator._consent_stop_deferred = True
    return "consent-stop deferred one turn (CHANGES DYNAMICS — re-run baselines)"


def unscoreable_cells(results) -> list[dict]:
    """FAULT 2 — find cells where the user granted consent and stopped in one turn.

    The agent is handed "Yes, please proceed with all three bookings. ###STOP###" and
    the conversation ends on that message. It never gets a turn in which to proceed,
    so whatever it does or does not write to the database says nothing about the
    procedure it was following.

    Conservative by construction: the message must be the LAST in the conversation
    (so the agent truly had no reply), must carry consent, and must not read as a
    refusal. On the four 2026-09-15 runs this flags 2 cells out of 104 — both in ASOP
    arms, neither in stock prose, which is suggestive of gated agents sitting
    mid-confirmation more often, and far too small a sample to claim as a bias.
    """
    out = []
    for sim in getattr(results, "simulations", []) or []:
        messages = getattr(sim, "messages", None) or []
        if not messages:
            continue
        last = messages[-1]
        role = getattr(last, "role", None) or (last.get("role") if isinstance(last, dict) else None)
        content = getattr(last, "content", None) or (
            last.get("content") if isinstance(last, dict) else None
        )
        if role != "user" or not content or STOP_MARKER not in content:
            continue
        said = content.replace(STOP_MARKER, "").strip()
        if _REFUSAL.search(said) or not _CONSENT.search(said):
            continue
        out.append(
            {
                "task_id": getattr(sim, "task_id", None),
                "trial": getattr(sim, "trial", None),
                "said": said[:160],
            }
        )
    return out
