"""Registry - Document metadata tracking."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


@dataclass
class DocEntry:
    """A document in the registry."""

    path: str  # relative to repo root (e.g., "company/strategy/MISSION.md")
    title: str
    created_by: str  # agent name or "system"
    created_at: str  # ISO date
    updated_at: str  # ISO datetime
    status: str = "draft"  # draft, active, archived
    tags: list[str] = field(default_factory=list)
    area: str = ""  # top-level area (strategy, product, engineering, etc.)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> DocEntry:
        return cls(**data)


class Registry:
    """Document registry backed by .agentco/registry.json."""

    def __init__(self, registry_path: Path):
        self.path = registry_path
        self.entries: dict[str, DocEntry] = {}
        self._load()

    def _load(self) -> None:
        """Load registry from JSON file."""
        if self.path.exists():
            data = json.loads(self.path.read_text())
            self.entries = {k: DocEntry.from_dict(v) for k, v in data.items()}

    def _save(self) -> None:
        """Save registry to JSON file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v.to_dict() for k, v in self.entries.items()}
        self.path.write_text(json.dumps(data, indent=2, default=str))

    def register(self, entry: DocEntry) -> None:
        """Add or update a document entry."""
        self.entries[entry.path] = entry
        self._save()

    def unregister(self, path: str) -> bool:
        """Remove a document entry. Returns True if found."""
        if path in self.entries:
            del self.entries[path]
            self._save()
            return True
        return False

    def get(self, path: str) -> DocEntry | None:
        """Get a document entry by path."""
        return self.entries.get(path)

    def list_all(self) -> list[DocEntry]:
        """List all documents."""
        return list(self.entries.values())

    def list_by_area(self, area: str) -> list[DocEntry]:
        """List documents in a specific area."""
        return [e for e in self.entries.values() if e.area == area]

    def list_by_agent(self, agent: str) -> list[DocEntry]:
        """List documents created by a specific agent."""
        return [e for e in self.entries.values() if e.created_by == agent]

    def list_by_tag(self, tag: str) -> list[DocEntry]:
        """List documents with a specific tag."""
        return [e for e in self.entries.values() if tag in e.tags]

    def list_recent(self, limit: int = 10) -> list[DocEntry]:
        """List most recently updated documents."""
        sorted_entries = sorted(
            self.entries.values(), key=lambda e: e.updated_at, reverse=True
        )
        return sorted_entries[:limit]

    def search(self, query: str) -> list[DocEntry]:
        """Search documents by title, path, or tags (case-insensitive)."""
        q = query.lower()
        return [
            e
            for e in self.entries.values()
            if q in e.title.lower()
            or q in e.path.lower()
            or any(q in t.lower() for t in e.tags)
        ]


def parse_frontmatter(file_path: Path) -> dict[str, Any] | None:
    """Parse YAML frontmatter from a markdown file.

    Reads the file and extracts the YAML block between --- markers.
    Returns parsed dict or None if no frontmatter found.
    """
    try:
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    match = FRONTMATTER_PATTERN.match(text)
    if not match:
        return None

    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None

    if not isinstance(data, dict):
        return None

    return data


def _determine_area(path: str) -> str:
    """Extract area from a company/ relative path.

    E.g., 'company/strategy/MISSION.md' -> 'strategy'
         'company/engineering/backend/API.md' -> 'engineering'
         'company/README.md' -> ''
    """
    parts = Path(path).parts
    # Expected: ("company", "<area>", ...)
    if len(parts) >= 2 and parts[0] == "company":
        return parts[1] if len(parts) > 2 else ""
    return ""


def scan_company(company_path: Path, registry: Registry) -> int:
    """Scan company/ directory and update registry from file frontmatter.

    Finds all .md files under company/, parses frontmatter for metadata,
    determines area from path, and registers/updates each document.
    Returns count of documents registered.
    """
    count = 0
    repo_root = company_path.parent

    for md_file in sorted(company_path.rglob("*.md")):
        rel_path = str(md_file.relative_to(repo_root))
        frontmatter = parse_frontmatter(md_file)

        now = datetime.now(timezone.utc).isoformat()
        stat = md_file.stat()
        file_mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()

        if frontmatter:
            title = frontmatter.get("title", md_file.stem)
            created_by = frontmatter.get("created_by", "system")
            created_at = str(frontmatter.get("created_at", now))
            updated_at = str(frontmatter.get("updated_at", file_mtime))
            status = frontmatter.get("status", "draft")
            tags = frontmatter.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",")]
        else:
            title = md_file.stem
            created_by = "system"
            created_at = now
            updated_at = file_mtime
            status = "draft"
            tags = []

        area = _determine_area(rel_path)

        entry = DocEntry(
            path=rel_path,
            title=title,
            created_by=created_by,
            created_at=created_at,
            updated_at=updated_at,
            status=status,
            tags=tags,
            area=area,
        )
        registry.register(entry)
        count += 1

    return count
