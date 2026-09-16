#!/usr/bin/env python3
"""F7 probe — proves the ISC-7 shape loads with a bare file read, no LifeOS-specific parser.

Deliberately does not import anything from agentco_harness, does not use PyYAML, and does not
call any LifeOS tool. The only "parsing" is a hand-rolled split on the first '---'-delimited
frontmatter block and a `key: value` line reader — the minimum any consumer would write for
itself, not a dependency on LifeOS's own loader (`hooks/lib/identity.ts`,
`LIFEOS/TOOLS/PaiConfig.ts`).

Usage: python3 docs/lifeos-exit/f7-example/probe_bare_read.py
Exits non-zero if any file is missing a required key or has an empty body.
"""

from __future__ import annotations

import sys
from pathlib import Path

REQUIRED_KEYS = ("last_updated", "last_updated_by", "convention")

FILES = [
    "TELOS/TELOS.md",
    "TELOS/PRINCIPAL_TELOS.md",
    "PRINCIPAL/PRINCIPAL_IDENTITY.md",
    "DIGITAL_ASSISTANT/DA_IDENTITY.md",
    "CONFIG/OPERATIONAL_RULES.md",
    "PROJECTS.md",
]


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Bare-bones frontmatter split: first '---' block is flat `key: value` pairs, rest is body.

    No YAML library, no nested structures — this is deliberately as dumb as a consumer with
    zero LifeOS knowledge would write. It's sufficient because the real contract (see
    ../f7-telos-identity-schema.md) never nests anything under pai-freshness-v1 keys.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm_lines = lines[1:i]
            body = "\n".join(lines[i + 1 :]).strip()
            fm: dict[str, str] = {}
            for line in fm_lines:
                if ":" in line:
                    key, _, value = line.partition(":")
                    fm[key.strip()] = value.strip()
            return fm, body
    return {}, text


def main() -> int:
    root = Path(__file__).parent
    failures = 0
    for rel in FILES:
        path = root / rel
        text = path.read_text(encoding="utf-8")  # the entire "loader": stdlib file read
        fm, body = split_frontmatter(text)
        missing = [k for k in REQUIRED_KEYS if k not in fm]
        if missing:
            print(f"FAIL {rel}: missing required key(s) {missing}")
            failures += 1
        elif not body:
            print(f"FAIL {rel}: empty body after frontmatter")
            failures += 1
        else:
            print(f"OK   {rel}: frontmatter keys={sorted(fm)} body_chars={len(body)}")
    if failures:
        print(f"\n{failures} file(s) failed the bare-read shape check.")
        return 1
    print(f"\nAll {len(FILES)} files load with a bare read: fixed path, "
          f"pai-freshness-v1 frontmatter, non-empty prose body. No LifeOS parser used.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
