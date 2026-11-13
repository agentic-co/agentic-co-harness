"""The mechanical half of the clean room: what may leave a partition.

WHY THIS EXISTS
---------------
A harness learns things while it works: lessons, facts about the systems it
touched, proposed changes to procedures. A hub's shared layer may only receive
what has been stripped of the partition's specifics: internal names, people,
hosts, and anything secret-shaped. The hub's own design (ADR 0006 §3) puts a
blind validator, an aggregate check and batch approval on the hub side.
ADR 0015 §3 puts this half here, BEFORE the outbox line is written, because
the outbox file is a durable record on disk, and content that must not leave
should never be written to it.

This module is a judge, not a rewriter. The model writes the residual; code
decides whether it may leave. Silently rewriting a hit would produce text
nobody wrote and nobody reviewed, so a hit refuses the whole submission and
the writer tries again.

WHAT IT CHECKS
--------------
Built in (no configuration):
  * secret shapes (cloud keys, VCS and chat tokens, private-key headers, JWTs,
    bearer headers, ``password=``-style assignments),
  * e-mail addresses,
  * private-range IPv4 addresses.
From the partition's private list file:
  * ``internal_names``: product, project, customer and codename terms,
  * ``never_leave``: terms that no approval overrides (0006 §3),
  * ``people``: names of people,
  * ``internal_domains``: host suffixes; any host under one is refused.
List terms match case-insensitively on whole words, with any run of
whitespace standing for a space. Substring matching would refuse innocent
English and teach people to switch the check off.

WHERE THE LISTS LIVE
--------------------
First hit wins: an explicit path from the caller, then ``AGENTCO_CLEAN_ROOM``,
then ``clean-room.json`` beside the bead store, which is the same shape of
resolution ``egress.py`` uses for its route table. The file is per-partition
private configuration. It is never committed to a public repository, and the
tests in this repository plant fabricated names.

FAIL-CLOSED
-----------
A missing, unreadable or unknown-schema list file refuses every submission,
and so does a file whose ``internal_names`` is empty: a list that names
nothing is indistinguishable from a forgotten one.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

LISTS_NAME = "clean-room.json"
LISTS_ENV = "AGENTCO_CLEAN_ROOM"
_SUPPORTED_SCHEMA = 1

CODE_UNAVAILABLE = "clean_room_unavailable"
CODE_REFUSED = "clean_room_refused"
CODE_SECRET = "clean_room_secret"
CODE_EMAIL = "clean_room_email"
CODE_HOST = "clean_room_host"
CODE_INTERNAL_NAME = "clean_room_internal_name"
CODE_NEVER_LEAVE = "clean_room_never_leave"
CODE_PERSON = "clean_room_person"


class CleanRoomUnavailable(Exception):
    """The list file is missing or unusable, so nothing may leave."""

    code = CODE_UNAVAILABLE


@dataclass(frozen=True)
class Hit:
    code: str
    rule: str
    path: str
    #: A masked rendering of the match. The matched text itself is never kept,
    #: because a refusal is logged and a logged secret has already leaked.
    masked: str

    def as_dict(self) -> dict:
        return {"code": self.code, "rule": self.rule, "path": self.path, "masked": self.masked}


class CleanRoomRefused(Exception):
    code = CODE_REFUSED

    def __init__(self, hits: list[Hit]):
        self.hits = hits
        summary = ", ".join(f"{h.code}@{h.path}" for h in hits[:10])
        more = f" (+{len(hits) - 10} more)" if len(hits) > 10 else ""
        super().__init__(f"{CODE_REFUSED}: {len(hits)} hit(s): {summary}{more}")


@dataclass(frozen=True)
class Lists:
    internal_names: tuple[str, ...] = ()
    never_leave: tuple[str, ...] = ()
    people: tuple[str, ...] = ()
    internal_domains: tuple[str, ...] = ()
    _compiled: dict = field(default_factory=dict, compare=False, repr=False)

    def patterns(self) -> list[tuple[str, str, re.Pattern]]:
        if not self._compiled:
            out = []
            for code, rule, terms in (
                (CODE_NEVER_LEAVE, "never_leave", self.never_leave),
                (CODE_PERSON, "people", self.people),
                (CODE_INTERNAL_NAME, "internal_names", self.internal_names),
            ):
                for term in terms:
                    out.append((code, rule, _term_pattern(term)))
            for dom in self.internal_domains:
                d = re.escape(dom.strip().lstrip(".").lower())
                out.append((CODE_HOST, "internal_domains",
                            re.compile(rf"(?<![\w.-])(?:[\w-]+\.)*{d}(?![\w-])", re.I)))
            self._compiled["p"] = out
        return self._compiled["p"]


def _term_pattern(term: str) -> re.Pattern:
    words = [re.escape(w) for w in term.split()]
    if not words:
        raise CleanRoomUnavailable("clean-room list contains an empty term")
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.I)


# Built-in secret shapes. Each is deliberately specific: a broad entropy rule
# would refuse every hash and id a lesson legitimately mentions.
_SECRET_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_style_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}", re.I)),
    ("assignment", re.compile(
        r"\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret)"
        r"\s*[:=]\s*['\"]?[^\s'\"]{6,}", re.I)),
)

_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


def _mask(s: str) -> str:
    if len(s) <= 4:
        return "*" * len(s)
    return f"{s[:2]}…{len(s)}ch"


def _strings(obj: Any, path: str = "$") -> Iterator[tuple[str, str]]:
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            kp = f"{path}.{k}" if isinstance(k, str) and re.fullmatch(r"\w+", k) else f"{path}[{k!r}]"
            if isinstance(k, str):
                yield f"{kp}<key>", k
            yield from _strings(v, kp)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _strings(v, f"{path}[{i}]")


def check(payload: Any, lists: Lists) -> list[Hit]:
    """Every hit in ``payload``, in document order. Empty means it may leave."""
    hits: list[Hit] = []
    for path, text in _strings(payload):
        for rule, rx in _SECRET_RULES:
            for m in rx.finditer(text):
                hits.append(Hit(CODE_SECRET, rule, path, _mask(m.group(0))))
        for m in _EMAIL.finditer(text):
            hits.append(Hit(CODE_EMAIL, "email", path, _mask(m.group(0))))
        for m in _IPV4.finditer(text):
            try:
                ip = ipaddress.IPv4Address(m.group(0))
            except ValueError:
                continue
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                hits.append(Hit(CODE_HOST, "private_ip", path, _mask(m.group(0))))
        for code, rule, rx in lists.patterns():
            for m in rx.finditer(text):
                hits.append(Hit(code, rule, path, _mask(m.group(0))))
    return hits


def list_path(explicit: Optional[str | Path] = None, *, store_dir: Optional[str | Path] = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(LISTS_ENV)
    if env:
        return Path(env).expanduser()
    return Path(store_dir or ".").expanduser() / LISTS_NAME


def _terms(raw: dict, key: str) -> tuple[str, ...]:
    v = raw.get(key) or []
    if not isinstance(v, list) or not all(isinstance(t, str) for t in v):
        raise CleanRoomUnavailable(f"{LISTS_NAME}: `{key}` must be a list of strings")
    return tuple(t for t in (s.strip() for s in v) if t)


def load_lists(path: str | Path) -> Lists:
    p = Path(path)
    try:
        raw = json.loads(p.read_text())
    except FileNotFoundError as e:
        raise CleanRoomUnavailable(
            f"no {LISTS_NAME} at {p}; nothing may leave this partition until one exists "
            f"(set {LISTS_ENV} or place it beside the bead store)") from e
    except (OSError, json.JSONDecodeError) as e:
        raise CleanRoomUnavailable(f"{LISTS_NAME} at {p} is unreadable: {e}") from e
    if not isinstance(raw, dict) or raw.get("schemaVersion") != _SUPPORTED_SCHEMA:
        raise CleanRoomUnavailable(
            f"{LISTS_NAME} at {p}: schemaVersion must be {_SUPPORTED_SCHEMA}")
    lists = Lists(
        internal_names=_terms(raw, "internal_names"),
        never_leave=_terms(raw, "never_leave"),
        people=_terms(raw, "people"),
        internal_domains=_terms(raw, "internal_domains"),
    )
    if not lists.internal_names:
        raise CleanRoomUnavailable(
            f"{LISTS_NAME} at {p} names no internal_names; an empty list cannot be "
            f"told apart from a forgotten one")
    lists.patterns()  # compile now, so a bad term refuses at load, not mid-check
    return lists


def assert_clean(payload: Any, lists: Optional[Lists] = None, *,
                 path: Optional[str | Path] = None,
                 store_dir: Optional[str | Path] = None) -> None:
    """Raise unless ``payload`` may leave. Loads the lists if none are given."""
    if lists is None:
        lists = load_lists(list_path(path, store_dir=store_dir))
    hits = check(payload, lists)
    if hits:
        raise CleanRoomRefused(hits)
