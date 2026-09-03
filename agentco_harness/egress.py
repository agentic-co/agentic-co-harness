"""Data-classification gate for cross-vendor bead dispatch.

WHY THIS EXISTS
---------------
LifeOS carries a data-classification ceiling model (``LIFEOS/TOOLS/models.ts``)
enforced by ``EgressClassGuard.hook.ts`` — a Claude Code PreToolUse Bash gate.
That gate only fires inside an interactive Claude Code session. AgentCo runs
unattended under launchd and dispatches beads to z.ai (a PUBLIC-ceiling vendor)
with no classification awareness whatsoever, which inverts the risk: the
supervised path was guarded and the unsupervised one was not.

This module closes that gap. It does NOT restate the policy — that would be a
second source of truth guaranteed to drift. It consumes the artifact emitted by
``LIFEOS/TOOLS/ExportInferenceRoutes.ts``, which is generated from models.ts.

FAIL-CLOSED
-----------
If the policy artifact is missing or unreadable, every non-native route is
denied. The native (Anthropic) route stays available without the artifact: it
is the RESTRICTED-capable home vendor, and bricking it would take the whole
system down over a missing advisory file — a cure worse than the disease.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

DataClass = Literal["RESTRICTED", "CONFIDENTIAL", "INTERNAL", "PUBLIC"]

#: Sensitivity ordinal — lower is MORE sensitive. Mirrors models.ts CLASS_RANK.
#: Duplicated here only as a bootstrap for the fail-closed path; the loaded
#: artifact's own ``classRank`` wins whenever it is available.
CLASS_RANK: dict[str, int] = {
    "RESTRICTED": 0,
    "CONFIDENTIAL": 1,
    "INTERNAL": 2,
    "PUBLIC": 3,
}

#: AgentCo's ``agent:`` value -> the LifeOS route it egresses through.
#: Extend this (and only this) when a new vendor leg lands in executor.py.
#:
#: NOT wired, deliberately — both fail unattended, and a route that fails at
#: 01:00 every night is worse than no route:
#:   bellows (Google/agy) — OAuth-only, no headless auth path (confirmed
#:       2026-07-24). Interactive sessions only.
#:   anvil (Moonshot/Kimi) — no MOONSHOT_API_KEY in ~/.claude/.env.
#: Add either the moment its auth story supports an unattended run.
AGENT_ROUTE: dict[str, str] = {
    "claude": "NATIVE",
    "zai": "TEMPER",
    "forge": "FORGE",
    "planner": "NATIVE",
}

#: Default class when a bead does not declare one. Deliberately conservative:
#: a store-backed subagent holds broad filesystem + store access, so a bead's
#: real blast radius is its *agent's reach*, not just its nominal input. A
#: genuinely public workload declares ``data_class: PUBLIC`` explicitly in its
#: recurring def — an auditable, in-git escape hatch rather than a silent one.
DEFAULT_CLASS_PERSONAL: DataClass = "INTERNAL"
DEFAULT_CLASS_COMPANY: DataClass = "CONFIDENTIAL"

_ARTIFACT_ENV = "LIFEOS_INFERENCE_ROUTES"
_ARTIFACT_DEFAULT = Path.home() / ".claude/LIFEOS/MEMORY/STATE/inference-routes.json"
_SUPPORTED_SCHEMA = 1


class EgressDenied(Exception):
    """A bead's data class exceeds the ceiling of the route it was sent to."""


class PolicyUnavailable(Exception):
    """The route artifact is missing or malformed and the route is not native."""


@dataclass(frozen=True)
class Route:
    name: str
    vendor: str
    model: str
    ceiling: DataClass
    ceiling_unsupervised: DataClass
    ceiling_verified: bool


def artifact_path() -> Path:
    """Resolve the policy artifact path (env override wins, for tests)."""
    override = os.environ.get(_ARTIFACT_ENV)
    return Path(override) if override else _ARTIFACT_DEFAULT


def load_routes(path: Optional[Path] = None) -> dict[str, Route]:
    """Load the exported route table.

    Raises PolicyUnavailable on anything unreadable, absent, or shape-shifted —
    callers translate that into a denial for non-native routes.
    """
    p = path or artifact_path()
    try:
        raw = json.loads(p.read_text())
    except FileNotFoundError as e:
        raise PolicyUnavailable(
            f"inference-routes.json not found at {p} — regenerate with "
            f"`bun ~/.claude/LIFEOS/TOOLS/ExportInferenceRoutes.ts`"
        ) from e
    except (OSError, json.JSONDecodeError) as e:
        raise PolicyUnavailable(f"inference-routes.json at {p} is unreadable: {e}") from e

    schema = raw.get("schemaVersion")
    if schema != _SUPPORTED_SCHEMA:
        raise PolicyUnavailable(
            f"inference-routes.json schemaVersion={schema!r}, this build supports "
            f"{_SUPPORTED_SCHEMA} — upgrade agentco or regenerate the artifact"
        )

    routes: dict[str, Route] = {}
    for name, r in (raw.get("routes") or {}).items():
        try:
            routes[name] = Route(
                name=name,
                vendor=r["vendor"],
                model=r["model"],
                ceiling=r["ceiling"],
                ceiling_unsupervised=r["ceilingUnsupervised"],
                ceiling_verified=bool(r["ceilingVerified"]),
            )
        except KeyError as e:
            raise PolicyUnavailable(f"route {name!r} is missing field {e}") from e
    if not routes:
        raise PolicyUnavailable(f"inference-routes.json at {p} declares no routes")
    return routes


def resolve_data_class(task_metadata: dict) -> DataClass:
    """Determine a bead's data class.

    Explicit ``data_class`` wins; otherwise derive from ``company``. Unknown
    values raise rather than silently downgrading to something permissive.
    """
    declared = (task_metadata or {}).get("data_class")
    if declared is not None:
        key = str(declared).strip().upper()
        if key not in CLASS_RANK:
            raise EgressDenied(
                f"bead declares unknown data_class {declared!r} — "
                f"expected one of {', '.join(CLASS_RANK)}"
            )
        return key  # type: ignore[return-value]

    company = (task_metadata or {}).get("company")
    if company is None or str(company).strip().lower() == "personal":
        return DEFAULT_CLASS_PERSONAL
    return DEFAULT_CLASS_COMPANY


def check_egress(
    agent: str,
    task_metadata: dict,
    *,
    routes: Optional[dict[str, Route]] = None,
    supervised: bool = False,
) -> tuple[DataClass, Optional[Route]]:
    """Authorize dispatching a bead to ``agent``. Raises on denial.

    Returns the resolved (data_class, route). ``route`` is None for the native
    path when the policy artifact is unavailable — the one tolerated gap, see
    the module docstring.

    ``supervised=False`` is the AgentCo default and is what makes an unverified
    ceiling (today: Bellows/Google) degrade one step.
    """
    data_class = resolve_data_class(task_metadata)
    route_name = AGENT_ROUTE.get(agent)

    if route_name is None:
        raise EgressDenied(
            f"agent {agent!r} has no declared egress route — add it to "
            f"agentco_harness.egress.AGENT_ROUTE before dispatching to it"
        )

    try:
        table = routes if routes is not None else load_routes()
    except PolicyUnavailable:
        if route_name == "NATIVE":
            return data_class, None
        raise

    route = table.get(route_name)
    if route is None:
        raise EgressDenied(
            f"route {route_name!r} (agent {agent!r}) is absent from the policy artifact"
        )

    ceiling = route.ceiling_unsupervised if not supervised else route.ceiling
    if CLASS_RANK[data_class] < CLASS_RANK[ceiling]:
        unverified = "" if route.ceiling_verified else (
            f" [ceiling unverified: nominal {route.ceiling}, degraded to {ceiling} "
            f"because this run is unsupervised]"
        )
        raise EgressDenied(
            f"bead classified {data_class} exceeds the {ceiling} ceiling of route "
            f"{route.name} ({route.vendor}/{route.model}){unverified}. "
            f"Either route this bead to a cleared vendor, or declare "
            f"data_class explicitly if {data_class} is wrong."
        )
    return data_class, route
