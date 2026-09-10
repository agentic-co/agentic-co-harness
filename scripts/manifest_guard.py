"""Prove a bulk file operation lost nothing.

Written from a real loss. On 2026-08-17 a 525-file restructure ran inside a
live two-way-synced Obsidian vault; six work-product notes disappeared and
were not in `.trash`. Detection was accidental, two days later.

The rule that came out of it is the whole of this tool: **verifying the moves
you INTENDED is not verification — diff what actually changed.** An operation
that reports success while having deleted something is the shape that hurt,
and it is the same shape as a gate that reports green having checked nothing.

Usage as a gate (exit 0 / non-zero, so it is a `deterministic` check):

    python3 scripts/manifest_guard.py --root ~/vault --glob '*.md' \\
        --expect-deleted 0 -- mv ~/vault/a.md ~/vault/b.md

Nothing here is Obsidian-specific: it is a directory, a glob, and a command.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from collections import Counter
from pathlib import Path


def manifest(root: Path, pattern: str) -> dict[str, str]:
    """Every matching file under `root`, keyed by relative path."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob(pattern)
        if p.is_file()
    }


def lost(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Paths whose CONTENT is gone — not merely paths that moved.

    Identity is the content hash, deliberately. A vault restructure IS a mass
    move: keying on path would flag every legitimate reorganisation, and a
    guard that cries wolf on the normal case is a guard somebody switches off,
    which is how you arrive back at no guard at all. A moved note keeps its
    bytes; a deleted note takes them with it.

    A Counter rather than a set because two notes can legitimately hold
    identical content, and losing one of a pair is still losing one.
    """
    surviving = Counter(after.values())
    gone = []
    for path, digest in sorted(before.items()):
        if surviving[digest] > 0:
            surviving[digest] -= 1
        else:
            gone.append(path)
    return gone


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--glob", default="*.md")
    ap.add_argument(
        "--expect-deleted",
        type=int,
        default=0,
        help="How many files this operation is ALLOWED to remove. Default 0: "
             "a bulk move should delete nothing, and the day it does you want "
             "to have said so in advance.",
    )
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)

    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print(f"manifest_guard: {root} is not a directory", file=sys.stderr)
        return 2
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not command:
        print("manifest_guard: no command given", file=sys.stderr)
        return 2

    before = manifest(root, args.glob)
    completed = subprocess.run(command)
    after = manifest(root, args.glob)

    deleted = lost(before, after)
    added = sorted(set(after) - set(before))

    print(f"manifest_guard: {len(before)} → {len(after)} "
          f"({len(added)} added, {len(deleted)} deleted)")
    for path in added:
        print(f"  + {path}")
    for path in deleted:
        print(f"  - {path}")

    if len(deleted) > args.expect_deleted:
        # Louder than the command's own exit status on purpose: the 2026-08-17
        # operation "succeeded". The loss is the finding, not the return code.
        print(
            f"manifest_guard: REFUSED — {len(deleted)} file(s) disappeared, "
            f"{args.expect_deleted} expected. Restore from the backup you took "
            f"before this ran.",
            file=sys.stderr,
        )
        return 1
    if completed.returncode != 0:
        print(
            f"manifest_guard: the command exited {completed.returncode}; "
            f"no files were lost.",
            file=sys.stderr,
        )
        return completed.returncode
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
