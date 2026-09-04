"""Company scaffold — creates a complete 'company in a repo' directory structure."""

from __future__ import annotations

from datetime import date
from pathlib import Path


def _frontmatter(
    title: str,
    *,
    status: str = "draft",
    tags: list[str] | None = None,
    related: list[str] | None = None,
) -> str:
    """Generate YAML frontmatter block."""
    tags = tags or []
    related = related or []
    created = date.today().isoformat()
    tag_str = ", ".join(tags)
    related_lines = "\n".join(f'  - "[[{r}]]"' for r in related)
    if related_lines:
        related_lines = "\n" + related_lines
    return (
        f"---\n"
        f"title: {title}\n"
        f"created_by: system\n"
        f"created_at: {created}\n"
        f"status: {status}\n"
        f"tags: [{tag_str}]\n"
        f"related: {related_lines or '[]'}\n"
        f"---\n"
    )


# ---------------------------------------------------------------------------
# Area definitions — each area has an INDEX template plus its files/dirs
# ---------------------------------------------------------------------------

_AREAS: dict[str, dict] = {
    "strategy": {
        "description": "Strategy & Market",
        "files": {
            "MISSION.md": {
                "title": "Mission Statement",
                "tags": ["strategy", "mission"],
                "related": ["strategy/VISION", "strategy/VALUES"],
                "body": (
                    "# Mission Statement\n\n"
                    "> Define your company's mission here.\n\n"
                    "## Why We Exist\n\n"
                    "## Who We Serve\n\n"
                    "## How We Create Value\n"
                ),
            },
            "VISION.md": {
                "title": "Vision Statement",
                "tags": ["strategy", "vision"],
                "related": ["strategy/MISSION", "strategy/VALUES"],
                "body": (
                    "# Vision Statement\n\n"
                    "> Where is the company headed?\n\n"
                    "## Long-Term Aspiration\n\n"
                    "## Success Looks Like\n"
                ),
            },
            "VALUES.md": {
                "title": "Company Values",
                "tags": ["strategy", "values"],
                "related": ["strategy/MISSION", "strategy/VISION"],
                "body": (
                    "# Company Values\n\n"
                    "## Core Values\n\n"
                    "1. **Value** - Description\n"
                ),
            },
            "ICP.md": {
                "title": "Ideal Customer Profile",
                "tags": ["strategy", "icp", "customer"],
                "related": ["strategy/positioning", "strategy/market/tam-sam-som"],
                "body": (
                    "# Ideal Customer Profile\n\n"
                    "## Demographics\n\n"
                    "## Pain Points\n\n"
                    "## Buying Triggers\n\n"
                    "## Decision Criteria\n"
                ),
            },
            "positioning.md": {
                "title": "Market Positioning",
                "tags": ["strategy", "positioning"],
                "related": ["strategy/ICP", "strategy/market/tam-sam-som"],
                "body": (
                    "# Market Positioning\n\n"
                    "## Positioning Statement\n\n"
                    "> For [target customer] who [need], [product] is a [category] "
                    "that [key benefit]. Unlike [alternative], we [differentiator].\n\n"
                    "## Competitive Advantage\n\n"
                    "## Key Differentiators\n"
                ),
            },
        },
        "subdirs": {
            "market": {
                "description": "Market Analysis",
                "files": {
                    "tam-sam-som.md": {
                        "title": "TAM / SAM / SOM Analysis",
                        "tags": ["strategy", "market", "tam"],
                        "related": ["strategy/ICP", "strategy/positioning"],
                        "body": (
                            "# TAM / SAM / SOM\n\n"
                            "## Total Addressable Market (TAM)\n\n"
                            "## Serviceable Addressable Market (SAM)\n\n"
                            "## Serviceable Obtainable Market (SOM)\n\n"
                            "## Methodology & Sources\n"
                        ),
                    },
                },
                "gitkeep_dirs": ["competitors", "trends"],
            },
        },
    },
    "product": {
        "description": "Product & Roadmap",
        "files": {
            "ROADMAP.md": {
                "title": "Product Roadmap",
                "tags": ["product", "roadmap"],
                "related": ["strategy/VISION", "product/backlog"],
                "body": (
                    "# Product Roadmap\n\n"
                    "## Current Quarter\n\n"
                    "## Next Quarter\n\n"
                    "## Long-Term Bets\n"
                ),
            },
            "backlog.md": {
                "title": "Product Backlog",
                "tags": ["product", "backlog"],
                "related": ["product/ROADMAP"],
                "body": (
                    "# Product Backlog\n\n"
                    "## Priority: Critical\n\n"
                    "## Priority: High\n\n"
                    "## Priority: Medium\n\n"
                    "## Priority: Low\n"
                ),
            },
        },
        "gitkeep_dirs": ["PRD", "features", "releases"],
    },
    "engineering": {
        "description": "Engineering & Architecture",
        "files": {
            "ARCHITECTURE.md": {
                "title": "System Architecture",
                "tags": ["engineering", "architecture"],
                "related": ["engineering/TECH_STACK"],
                "body": (
                    "# System Architecture\n\n"
                    "## Overview\n\n"
                    "## Component Diagram\n\n"
                    "## Data Flow\n\n"
                    "## Key Decisions\n"
                ),
            },
            "TECH_STACK.md": {
                "title": "Technology Stack",
                "tags": ["engineering", "stack"],
                "related": ["engineering/ARCHITECTURE"],
                "body": (
                    "# Technology Stack\n\n"
                    "## Languages & Frameworks\n\n"
                    "## Infrastructure\n\n"
                    "## Third-Party Services\n\n"
                    "## Development Tools\n"
                ),
            },
        },
        "gitkeep_dirs": ["adr", "sop", "runbooks", "sdlc"],
    },
    "operations": {
        "description": "Operations & Incidents",
        "files": {},
        "gitkeep_dirs": ["logs", "metrics", "incidents", "postmortems"],
    },
    "customer-success": {
        "description": "Customer Success",
        "files": {},
        "gitkeep_dirs": ["feedback", "tickets", "nps"],
    },
    "marketing": {
        "description": "Marketing & Content",
        "files": {},
        "gitkeep_dirs": ["messaging", "campaigns", "content", "analytics"],
    },
    "finance": {
        "description": "Finance & Projections",
        "files": {},
        "gitkeep_dirs": ["projections", "reports"],
    },
    "agents": {
        "description": "AI Agents",
        "files": {},
        "gitkeep_dirs": [],
    },
}


# ---------------------------------------------------------------------------
# Index generators
# ---------------------------------------------------------------------------

def _area_index(area_name: str, area: dict) -> str:
    """Generate an INDEX.md for a single area."""
    desc = area["description"]
    lines: list[str] = []

    for fname in sorted(area.get("files", {})):
        stem = fname.removesuffix(".md")
        title = area["files"][fname]["title"]
        lines.append(f"- [[{area_name}/{stem}]] - {title}")

    for sub_name, sub in sorted(area.get("subdirs", {}).items()):
        lines.append(f"- [[{area_name}/{sub_name}/INDEX]] - {sub['description']}")

    for d in sorted(area.get("gitkeep_dirs", [])):
        lines.append(f"- `{d}/` - *(empty — add files here)*")

    content = (
        _frontmatter(
            f"{desc} Index",
            status="active",
            tags=["index", area_name],
        )
        + f"\n# {desc}\n\n"
        + "\n".join(lines)
        + "\n"
    )
    return content


def _subdir_index(area_name: str, sub_name: str, sub: dict) -> str:
    """Generate an INDEX.md for a subdirectory."""
    desc = sub["description"]
    lines: list[str] = []

    for fname in sorted(sub.get("files", {})):
        stem = fname.removesuffix(".md")
        title = sub["files"][fname]["title"]
        lines.append(f"- [[{area_name}/{sub_name}/{stem}]] - {title}")

    for d in sorted(sub.get("gitkeep_dirs", [])):
        lines.append(f"- `{d}/` - *(empty — add files here)*")

    content = (
        _frontmatter(
            f"{desc} Index",
            status="active",
            tags=["index", area_name, sub_name],
        )
        + f"\n# {desc}\n\n"
        + "\n".join(lines)
        + "\n"
    )
    return content


def _root_index() -> str:
    """Generate the master company INDEX.md."""
    lines: list[str] = []
    for area_name, area in _AREAS.items():
        lines.append(f"- [[{area_name}/INDEX]] - {area['description']}")

    content = (
        _frontmatter(
            "Company Index",
            status="active",
            tags=["index"],
        )
        + "\n# Company Index\n\n"
        + "## Areas\n\n"
        + "\n".join(lines)
        + "\n"
    )
    return content


def _readme() -> str:
    """Generate company README.md."""
    return (
        _frontmatter(
            "Company Overview",
            status="draft",
            tags=["overview"],
        )
        + "\n# Company Overview\n\n"
        "> Replace this with a brief description of your company.\n\n"
        "## What We Do\n\n"
        "## How This Repo Works\n\n"
        "This repository is a structured knowledge base for running the company. "
        "Each top-level directory represents a functional area.\n\n"
        "See [[INDEX]] for a full map.\n"
    )


# ---------------------------------------------------------------------------
# File creation helpers
# ---------------------------------------------------------------------------

def _write(path: Path, content: str) -> None:
    """Write a file, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _gitkeep(directory: Path) -> None:
    """Create an empty directory with a .gitkeep sentinel."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".gitkeep").touch()


def _write_file_entry(base: Path, area_name: str, fname: str, meta: dict) -> None:
    """Write a single templated markdown file."""
    body = meta.get("body", "")
    content = (
        _frontmatter(
            meta["title"],
            tags=meta.get("tags", []),
            related=meta.get("related", []),
        )
        + "\n"
        + body
    )
    _write(base / area_name / fname, content)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scaffold_company(base_path: Path) -> Path:
    """Create the full company directory structure under *base_path*/company.

    Returns the path to the created ``company/`` directory.
    """
    root = base_path / "company"
    root.mkdir(parents=True, exist_ok=True)

    # Root-level files
    _write(root / "INDEX.md", _root_index())
    _write(root / "README.md", _readme())

    # Each area
    for area_name, area in _AREAS.items():
        area_dir = root / area_name
        area_dir.mkdir(parents=True, exist_ok=True)

        # Area INDEX
        _write(area_dir / "INDEX.md", _area_index(area_name, area))

        # Area files
        for fname, meta in area.get("files", {}).items():
            _write_file_entry(root, area_name, fname, meta)

        # Area gitkeep dirs
        for d in area.get("gitkeep_dirs", []):
            _gitkeep(area_dir / d)

        # Subdirectories (e.g. strategy/market)
        for sub_name, sub in area.get("subdirs", {}).items():
            sub_dir = area_dir / sub_name
            sub_dir.mkdir(parents=True, exist_ok=True)

            # Sub INDEX
            _write(sub_dir / "INDEX.md", _subdir_index(area_name, sub_name, sub))

            # Sub files
            for fname, meta in sub.get("files", {}).items():
                _write_file_entry(root, f"{area_name}/{sub_name}", fname, meta)

            # Sub gitkeep dirs
            for d in sub.get("gitkeep_dirs", []):
                _gitkeep(sub_dir / d)

    return root


def scaffold_agentco_runtime(base_path: Path) -> Path:
    """Create the .agentco/ runtime directory structure.

    Layout::

        .agentco/
        ├── config.yaml        # placeholder config
        ├── tasks.jsonl         # empty task log
        ├── credentials/        # API keys, tokens (gitignored)
        ├── optimized/          # DSPy-optimized prompts
        └── state/              # runtime state files

    Scaffolding is additive: re-running it on a live node must never destroy
    operator-written state. ``config.yaml`` is only written when absent — on
    2026-08-04 an ``agentco init --company`` on the already-live sommeliwhey node
    overwrote its config with the placeholder below, dropping ``instance:`` and
    the ``agents:`` block. That silently un-declared the externally-executed
    ``box-scout`` agent, so the next cycle claimed 50 of its beads and failed
    every one with "Unknown agent: box-scout" (see Orchestrator._external_agent).

    Returns the path to the created ``.agentco/`` directory.
    """
    runtime = base_path / ".agentco"
    runtime.mkdir(parents=True, exist_ok=True)

    # Placeholder config — only for a fresh node; never clobber a real one.
    config_path = runtime / "config.yaml"
    if not config_path.exists():
        config_content = (
            "# AgentCo Runtime Configuration\n"
            "# See documentation for available options.\n\n"
            "tasks_path: tasks.jsonl\n\n"
            "agents: {}\n\n"
            "llm:\n"
            "  default_provider: openai\n"
            "  default_model: gpt-4o-mini\n"
        )
        _write(config_path, config_content)

    # Empty tasks log
    (runtime / "tasks.jsonl").touch()

    # Runtime subdirectories
    for dirname in ("credentials", "optimized", "state"):
        _gitkeep(runtime / dirname)

    return runtime
