"""The operator's declared registries — who may verify, who may adjudicate.

ASOP §9 names these, and names them for a reason worth repeating here because
this runtime is the third implementation to get it wrong:

> Saying "the operator's declared registry" and stopping was the same mistake
> one level up: three implementations read that sentence and invented three
> answers.

This runtime invented a fourth: it read `$USER` and compared strings. That is
not authentication, it is a mistake detector — §5.3 says so directly, having
watched three implementations ship "differs from the executor" as the whole
check and call it verification.

**Fail closed, and this is where this runtime and the plane currently
disagree.** The spec states it twice — §5.3: *"An unset or absent registry
authenticates nobody, not everybody: the failure mode is closed"*; §9: *"A
registry with nothing declared authenticates nobody; there is no fallback that
authenticates everybody."* So that is what is implemented here.

The plane (`agentco/policy.py::verifiers_from_env`) deliberately fails OPEN on
an empty set, with a real argument: *"a registry where nobody may verify does
not become safer, it resolves every judged gate on the clock, which is work
approved on a timer."* That argument is not silly, and it is not this module's
to settle — it is finding 4, and it belongs in the spec rather than in whichever
implementation is edited last. Implementing the spec here makes the
disagreement visible and dated instead of latent.

The counter, for whoever settles it: a parked gate only becomes "approved on a
timer" when `on_timeout` is `pass`. With `fail` or `escalate` a closed registry
parks work and then says so, which is the deterministic gate's posture — a
check that cannot run is not silently a pass.
"""

from __future__ import annotations

import os

# The standard's names. The `AGENTCO_*` spellings are the pre-split ones and
# still work, the same courtesy the plane extends, so a deployment that
# predates the rename keeps running. Nothing new is written against them.
VERIFIERS_ENV_VAR = "ASOP_VERIFIERS"
LEGACY_VERIFIERS_ENV_VAR = "AGENTCO_VERIFIERS"
ADJUDICATORS_ENV_VAR = "ASOP_ADJUDICATORS"
LEGACY_ADJUDICATORS_ENV_VAR = "AGENTCO_ADJUDICATORS"


class Unauthenticated(ValueError):
    """An identity that does not resolve against the declared registry.

    Its own exception type because §10 gives it its own refusal code, and
    because a caller distinguishing "not allowed" from "malformed" cannot do it
    by reading a message.
    """

    code = "unauthenticated"


def _declared(name: str, legacy: str) -> str | None:
    """The standard's variable, falling back to the pre-split name.

    Presence, not truthiness — an operator who deliberately declares an EMPTY
    set means something by it, and falling through to the legacy name would
    silently override that. Copied in shape from the plane's `_declared` on
    purpose: two readers of one declaration that disagree about what "unset"
    means is the divergence this whole effort exists to remove.
    """
    found = os.environ.get(name)
    return found if found is not None else os.environ.get(legacy)


def _split(value: str | None) -> frozenset[str]:
    if not value:
        return frozenset()
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def verifiers() -> frozenset[str]:
    """Actors the operator declared may answer a judged or human gate."""
    return _split(_declared(VERIFIERS_ENV_VAR, LEGACY_VERIFIERS_ENV_VAR))


def adjudicators() -> frozenset[str]:
    """Actors the operator declared may judge a divergence (§6.1)."""
    return _split(_declared(ADJUDICATORS_ENV_VAR, LEGACY_ADJUDICATORS_ENV_VAR))


def authenticate(actor: str | None, declared: frozenset[str], *, role: str) -> str:
    """Resolve `actor` against `declared`, or raise `Unauthenticated`.

    Returns the actor so a caller can use this inline at the point of record,
    which is the only place it is worth doing: §9 puts authentication "at the
    same choke point that flips a bead's status, not a convention the caller is
    trusted to honour."
    """
    if not actor:
        raise Unauthenticated(
            f"no {role} named — an unnamed identity resolves against nothing"
        )
    if not declared:
        raise Unauthenticated(
            f"{actor!r} cannot be authenticated as a {role}: no registry is "
            f"declared, and an unset registry authenticates nobody rather than "
            f"everybody (ASOP §5.3, §9). Declare {VERIFIERS_ENV_VAR} "
            f"(or {ADJUDICATORS_ENV_VAR}) with the actors permitted to answer."
        )
    if actor not in declared:
        raise Unauthenticated(
            f"{actor!r} is not in the declared {role} registry "
            f"({', '.join(sorted(declared))}). Naming an identity is not "
            f"authenticating it."
        )
    return actor
