"""PRIME — the per-node context cache.

`agentco prime` writes a `PRIME.md` next to a node's `config.yaml`: the
pointers an agent needs to orient in this repo before it starts work, so every
bead does not pay the same rediscovery cost.

Two rules make it safe to inject into prompts:

1. **Extractive pointers only.** Paths, script names, commit subjects, one
   purpose line quoted verbatim from the repo's own README. Never a paraphrase
   and never a derived "fact" — a poisoned PRIME multiplies into every bead
   executed against the node, and a wrong summary is indistinguishable from a
   right one at read time. Everything here is deterministically checkable by
   opening the path it names.

2. **Content-stamped, not time-stamped.** The stamp records git HEAD plus a
   SHA-256 of every source document read. Freshness is "HEAD is unchanged and
   no source has changed", not "younger than N days" — a 7-day window misses
   the same-day commit that invalidated it, which is exactly when a stale cache
   does damage.

No LLM anywhere in this module.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PRIME_FILENAME = "PRIME.md"

# Documents whose CONTENT is read into PRIME (and therefore hashed into the
# stamp). Order is the order they are considered for the purpose line.
SOURCE_DOCS = ("README.md", "CLAUDE.md", "ISA.md", "pyproject.toml", "package.json")

# Paths merely POINTED AT (not read into the body) still get listed.
POINTER_DOCS = ("CLAUDE.md", "README.md", "ISA.md", "AGENTS.md")

# Directories that carry no orientation value and would swamp the tree.
TREE_SKIP = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build", ".idea", ".vscode",
    ".claude", "target", ".next", "coverage", "htmlcov", ".tox", "site-packages",
}

MAX_TREE_ENTRIES = 60  # total lines in the tree section
MAX_CHILDREN_PER_DIR = 8
MAX_COMMITS = 5
MAX_PLAN_FILES = 10

# How much PRIME may occupy in a bead prompt. Head-truncated: the top of the
# file is the orientation (purpose, tree, entry points); the tail is history.
PRIME_INJECT_MAX_BYTES = 4096

_STAMP_OPEN = "<!-- agentco-prime-stamp"
_STAMP_CLOSE = "-->"
_STAMP_RE = re.compile(
    re.escape(_STAMP_OPEN) + r"\s*(?P<json>\{.*?\})\s*" + re.escape(_STAMP_CLOSE),
    re.DOTALL,
)


class PrimeError(Exception):
    """Raised when PRIME cannot be generated or read at all."""


@dataclass
class PrimeStamp:
    """The content fingerprint that decides freshness."""

    generated_at: str
    git_head: str | None
    sources: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "generated_at": self.generated_at,
                "git_head": self.git_head,
                "sources": self.sources,
            },
            indent=2,
            sort_keys=True,
        )


def _git(args: list[str], cwd: Path) -> str | None:
    """Run a read-only git command; None when this is not a repo / git is absent."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, NotADirectoryError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def node_dir(config) -> Path:
    """The directory a node's PRIME.md belongs in — where its config.yaml lives.

    Falls back to the store's directory for a defaults-only config (no file on
    disk), which is the same place every other per-node artifact lands.
    """
    if getattr(config, "config_path", None):
        return Path(config.config_path).resolve().parent
    return Path(config.tasks_path).resolve().parent


def prime_path(config) -> Path:
    return node_dir(config) / PRIME_FILENAME


def scan_root(directory: Path) -> Path:
    """What PRIME describes: the enclosing git repo, else the node dir itself.

    A node living in `.agentco/` is describing its REPO, not its own two config
    files — that is the context an agent working a bead there actually needs.
    """
    top = _git(["rev-parse", "--show-toplevel"], directory)
    if top:
        candidate = Path(top)
        if candidate.is_dir():
            return candidate
    return directory


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_hashes(root: Path) -> dict[str, str]:
    """SHA-256 of every source document that exists, keyed by relative path."""
    hashes: dict[str, str] = {}
    for name in SOURCE_DOCS:
        candidate = root / name
        if candidate.is_file():
            try:
                hashes[name] = _sha256(candidate)
            except OSError:
                continue
    return hashes


def _purpose_line(root: Path) -> tuple[str, str] | None:
    """First substantive prose line of README/CLAUDE, quoted verbatim.

    Returns (source_filename, line). Headings, badges and blockquotes are
    skipped — a title is a name, not a purpose.
    """
    for name in ("README.md", "CLAUDE.md"):
        doc = root / name
        if not doc.is_file():
            continue
        try:
            text = doc.read_text(errors="replace")
        except OSError:
            continue
        for raw in text.splitlines()[:60]:
            line = raw.strip()
            if not line or line.startswith(("#", ">", "!", "[!", "---", "```", "|")):
                continue
            return name, line[:400]
    return None


def _tree(root: Path) -> list[str]:
    """Two-level listing of the repo, capped. Directories first, alphabetical."""
    lines: list[str] = []
    try:
        top = sorted(
            (p for p in root.iterdir() if p.name not in TREE_SKIP and not p.name.startswith(".")),
            key=lambda p: (not p.is_dir(), p.name.lower()),
        )
    except OSError as e:
        raise PrimeError(f"cannot read {root}: {e}") from e

    for entry in top:
        if len(lines) >= MAX_TREE_ENTRIES:
            lines.append("… (truncated)")
            break
        if entry.is_dir():
            lines.append(f"{entry.name}/")
            try:
                children = sorted(
                    (
                        c
                        for c in entry.iterdir()
                        if c.name not in TREE_SKIP and not c.name.startswith(".")
                    ),
                    key=lambda p: (not p.is_dir(), p.name.lower()),
                )
            except OSError:
                continue
            for child in children[:MAX_CHILDREN_PER_DIR]:
                if len(lines) >= MAX_TREE_ENTRIES:
                    break
                lines.append(f"  {child.name}{'/' if child.is_dir() else ''}")
            if len(children) > MAX_CHILDREN_PER_DIR:
                lines.append(f"  … +{len(children) - MAX_CHILDREN_PER_DIR} more")
        else:
            lines.append(entry.name)
    return lines


def _entry_points(root: Path) -> list[str]:
    """Detected ways to RUN this thing, each naming the file it came from."""
    found: list[str] = []

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib

            data = tomllib.loads(pyproject.read_text(errors="replace"))
        except Exception:  # noqa: BLE001 — a malformed manifest is not fatal here
            data = {}
        scripts = (data.get("project") or {}).get("scripts") or {}
        for name, target in sorted(scripts.items()):
            found.append(f"`{name}` → `{target}` (pyproject.toml [project.scripts])")
        optional = (data.get("project") or {}).get("optional-dependencies") or {}
        if "dev" in optional:
            found.append("tests: `uv run --extra dev pytest` (pyproject.toml dev extra)")

    package_json = root / "package.json"
    if package_json.is_file():
        try:
            pkg = json.loads(package_json.read_text(errors="replace"))
        except (json.JSONDecodeError, OSError):
            pkg = {}
        for name in sorted((pkg.get("scripts") or {})):
            found.append(f"`{name}` (package.json scripts)")
        if pkg.get("main"):
            found.append(f"main → `{pkg['main']}` (package.json)")

    for candidate in ("main.py", "app.py", "index.ts", "index.js", "src/main.ts", "src/index.ts"):
        if (root / candidate).is_file():
            found.append(f"module → `{candidate}`")
    return found


def _key_paths(root: Path) -> list[str]:
    """Where the standing context lives. Paths only — the agent opens them."""
    paths: list[str] = []
    for name in POINTER_DOCS:
        if (root / name).is_file():
            paths.append(f"`{name}`")
    plans = root / "Plans"
    if plans.is_dir():
        try:
            plan_files = sorted(p.name for p in plans.glob("*.md"))
        except OSError:
            plan_files = []
        shown = ", ".join(f"`Plans/{p}`" for p in plan_files[:MAX_PLAN_FILES])
        extra = f" (+{len(plan_files) - MAX_PLAN_FILES} more)" if len(plan_files) > MAX_PLAN_FILES else ""
        paths.append(f"`Plans/` — {shown}{extra}" if shown else "`Plans/`")
    return paths


def _recent_commits(root: Path) -> list[str]:
    log = _git(["log", f"-{MAX_COMMITS}", "--format=%h %s"], root)
    if not log:
        return []
    return [line for line in log.splitlines() if line.strip()]


def render(directory: Path, now: datetime | None = None) -> str:
    """Build the PRIME.md body for the node rooted at `directory`."""
    root = scan_root(directory)
    head = _git(["rev-parse", "HEAD"], root)
    stamp = PrimeStamp(
        generated_at=(now or datetime.now(timezone.utc)).isoformat(),
        git_head=head,
        sources=_source_hashes(root),
    )

    out: list[str] = [
        f"# PRIME — {root.name}",
        "",
        "> Generated by `agentco prime`. Extractive pointers only — every line "
        "below is a path, a name, or text quoted verbatim from this repo. "
        "Nothing here is inferred. Open the paths for the real thing.",
        "",
    ]

    purpose = _purpose_line(root)
    out.append("## Purpose")
    out.append("")
    if purpose:
        source, line = purpose
        out.append(f"{line}")
        out.append("")
        out.append(f"— quoted from `{source}`")
    else:
        out.append("_No README.md or CLAUDE.md prose line found._")
    out.append("")

    out.append("## Key paths")
    out.append("")
    key_paths = _key_paths(root)
    if key_paths:
        out.extend(f"- {p}" for p in key_paths)
    else:
        out.append("_none found_")
    out.append("")

    out.append("## Entry points")
    out.append("")
    entries = _entry_points(root)
    if entries:
        out.extend(f"- {e}" for e in entries)
    else:
        out.append("_none detected_")
    out.append("")

    out.append("## Tree (2 levels)")
    out.append("")
    out.append("```")
    out.extend(_tree(root))
    out.append("```")
    out.append("")

    out.append("## Recent commits")
    out.append("")
    commits = _recent_commits(root)
    if commits:
        out.extend(f"- {c}" for c in commits)
    else:
        out.append("_not a git repo_")
    out.append("")

    out.append("## Stamp")
    out.append("")
    out.append(
        "Freshness is content-based: PRIME is stale the moment HEAD moves or "
        "any hashed source changes. `agentco prime --check` decides; "
        "`agentco prime` refreshes."
    )
    out.append("")
    out.append(_STAMP_OPEN)
    out.append(stamp.to_json())
    out.append(_STAMP_CLOSE)
    out.append("")
    return "\n".join(out)


def write(directory: Path, now: datetime | None = None) -> Path:
    """Generate and write PRIME.md into `directory`. Returns the path."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / PRIME_FILENAME
    target.write_text(render(directory, now=now))
    return target


def read_stamp(prime_file: Path) -> PrimeStamp:
    """Parse the stamp out of a PRIME.md. Raises PrimeError on anything odd."""
    try:
        text = prime_file.read_text(errors="replace")
    except OSError as e:
        raise PrimeError(f"cannot read {prime_file}: {e}") from e
    match = _STAMP_RE.search(text)
    if not match:
        raise PrimeError(
            f"{prime_file} carries no agentco-prime-stamp — it was not generated "
            f"by `agentco prime` (or was hand-edited). Regenerate it."
        )
    try:
        data = json.loads(match.group("json"))
    except json.JSONDecodeError as e:
        raise PrimeError(f"{prime_file} has an unparseable stamp: {e}") from e
    return PrimeStamp(
        generated_at=str(data.get("generated_at", "")),
        git_head=data.get("git_head"),
        sources=dict(data.get("sources") or {}),
    )


def check(directory: Path) -> tuple[bool, list[str]]:
    """Is the PRIME.md in `directory` still true? Returns (fresh, reasons).

    Reasons are always populated when not fresh — "stale" with no cause is not
    actionable, and the caller prints them verbatim.
    """
    directory = Path(directory)
    target = directory / PRIME_FILENAME
    if not target.is_file():
        return False, [f"no {PRIME_FILENAME} at {directory} — run `agentco prime`"]

    stamp = read_stamp(target)
    root = scan_root(directory)
    reasons: list[str] = []

    head = _git(["rev-parse", "HEAD"], root)
    if head != stamp.git_head:
        reasons.append(
            f"git HEAD moved: stamped {(stamp.git_head or 'none')[:12]} → "
            f"now {(head or 'none')[:12]}"
        )

    current = _source_hashes(root)
    for name, digest in sorted(stamp.sources.items()):
        if name not in current:
            reasons.append(f"source removed: {name}")
        elif current[name] != digest:
            reasons.append(f"source changed: {name}")
    for name in sorted(set(current) - set(stamp.sources)):
        reasons.append(f"source added since generation: {name}")

    return (not reasons), reasons


def injection_block(config_path: str | Path | None) -> str:
    """PRIME content to prepend to a bead prompt, or "" when there is none.

    Capped and HEAD-truncated: the top of the file is orientation (purpose,
    paths, entry points), the tail is commit history, so keeping the head keeps
    the useful part. Truncation is announced in the text — a silently cut
    context reads as a complete one.
    """
    if not config_path:
        return ""
    target = Path(config_path).resolve().parent / PRIME_FILENAME
    if not target.is_file():
        return ""
    try:
        text = target.read_text(errors="replace")
    except OSError:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) > PRIME_INJECT_MAX_BYTES:
        text = encoded[:PRIME_INJECT_MAX_BYTES].decode("utf-8", errors="ignore")
        text += (
            f"\n\n[PRIME truncated at {PRIME_INJECT_MAX_BYTES} bytes — "
            f"read the full file at {target} if you need the rest]"
        )
    return (
        "Node context (cached by `agentco prime` — pointers, not conclusions; "
        "open the paths to confirm anything you rely on):\n\n"
        f"{text}\n\n---\n\n"
    )
