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
second source of truth guaranteed to drift. It consumes an exported route
table: a JSON artifact naming each route's vendor, model and ceiling. The
deployment this came from generates it from models.ts; nothing here requires
that, and the format is documented by ``load_routes`` alone.

WHERE THE ARTIFACT LIVES
------------------------
Resolved in this order, first hit wins:

1. an explicit path passed by the caller (``config.egress.routes_path``),
2. the ``AGENTCO_INFERENCE_ROUTES`` environment variable, or its pre-extraction
   name ``LIFEOS_INFERENCE_ROUTES``,
3. ``inference-routes.json`` beside the bead store.

Nothing else. (3) is the point: the default is a file the runtime owns, next
to the queue it guards, rather than a path inside one operator's home. That
default used to be ``~/.claude/LIFEOS/MEMORY/STATE`` — for anybody who is not
that operator, a file that never exists, so every non-native route was denied
by a fail-closed rule doing exactly what it promised for a reason that had
nothing to do with them.

The first replacement kept that legacy path as a silent fourth fallback, so
an upgraded install would not start denying routes it allowed yesterday. A
cross-vendor review (2026-09-04) pointed out what that costs: a brand-new
project on a machine that still carries an old global table inherits that
table without anyone choosing it — a fresh install and an upgraded one are
indistinguishable to a security gate. So the upgrade signal is explicit now:
an upgraded deployment sets the environment variable, or ``egress.routes_path``,
and says so. A fresh project with neither fails closed, which is the answer
a route table it never had should give.

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

_ARTIFACT_ENV = "AGENTCO_INFERENCE_ROUTES"

#: The name this variable had while the module lived inside LifeOS. Still
#: read, second, because renaming an environment variable that currently
#: points a live deployment at its route table would not fail — it would
#: fail CLOSED, denying every non-native route, which is the same symptom
#: as a correctly-working guard and therefore the expensive kind of silent.
_ARTIFACT_ENV_LEGACY = "LIFEOS_INFERENCE_ROUTES"

#: The artifact's basename, resolved beside the bead store by default.
ARTIFACT_NAME = "inference-routes.json"
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


def artifact_path(
    explicit: Optional[str | Path] = None,
    *,
    store_dir: Optional[str | Path] = None,
) -> Path:
    """Resolve the policy artifact path. See the module docstring for order.

    ``explicit`` is the configured path (``config.egress.routes_path``);
    ``store_dir`` is the directory holding the bead store, which is where the
    default lives. With no store directory either, the default is the artifact
    name relative to the working directory — the caller that has no store has
    no better answer, and a relative name at least says so in the error.
    """
    if explicit:
        return Path(explicit).expanduser()

    override = os.environ.get(_ARTIFACT_ENV) or os.environ.get(_ARTIFACT_ENV_LEGACY)
    if override:
        return Path(override).expanduser()

    base = Path(store_dir).expanduser() if store_dir is not None else Path(".")
    return base / ARTIFACT_NAME


def load_routes(
    path: Optional[Path] = None,
    *,
    store_dir: Optional[str | Path] = None,
) -> dict[str, Route]:
    """Load the exported route table.

    Raises PolicyUnavailable on anything unreadable, absent, or shape-shifted —
    callers translate that into a denial for non-native routes.
    """
    p = Path(path) if path is not None else artifact_path(store_dir=store_dir)
    try:
        raw = json.loads(p.read_text())
    except FileNotFoundError as e:
        raise PolicyUnavailable(
            f"{ARTIFACT_NAME} not found at {p}. Point `egress.routes_path` in "
            f"config.yaml at an exported route table, set "
            f"{_ARTIFACT_ENV}, or place one there. Until then every "
            f"non-native route is denied and the native route still runs."
        ) from e
    except (OSError, json.JSONDecodeError) as e:
        raise PolicyUnavailable(f"{ARTIFACT_NAME} at {p} is unreadable: {e}") from e

    schema = raw.get("schemaVersion")
    if schema != _SUPPORTED_SCHEMA:
        raise PolicyUnavailable(
            f"{ARTIFACT_NAME} schemaVersion={schema!r}, this build supports "
            f"{_SUPPORTED_SCHEMA} — upgrade the runtime or regenerate the artifact"
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
        raise PolicyUnavailable(f"{ARTIFACT_NAME} at {p} declares no routes")
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
    routes_path: Optional[str | Path] = None,
    store_dir: Optional[str | Path] = None,
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
        if routes is not None:
            table = routes
        else:
            table = load_routes(
                artifact_path(routes_path, store_dir=store_dir), store_dir=store_dir
            )
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
