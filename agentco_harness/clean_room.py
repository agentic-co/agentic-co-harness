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
List terms match on whole words, with any run of whitespace standing for a
space. Before matching, text is NFKD-normalised, stripped of combining
marks, variation selectors and format characters (zero-width spaces, soft
hyphens), case-folded and has common Cyrillic and Greek look-alike letters
mapped to Latin. A string longer than MAX_STRING_CHARS is refused unscanned. ``_`` counts as a word
boundary, so ``name_prod`` and ``PROD_NAME`` are caught. Substring matching
would refuse innocent English and teach people to switch the check off.

Only JSON-shaped data is judged. A non-JSON leaf (bytes, a set, an object),
a non-string key, or nesting past ``MAX_DEPTH`` (which a cycle reaches) is a
hit, not an exception. A key that is itself a hit is never rendered into a
path; its position stands in for it.

KNOWN LIMITS (the hub's blind validator exists for these)
--------------------------------------------------------
* Matches never cross fields: a name split over two values passes.
* Word order is literal: "Last, First" does not match "First Last".
* Plurals and glued forms (``NamesApp``) pass; there is no substring mode yet.
* The look-alike map covers common Cyrillic and Greek letters, not the full
  Unicode confusables table.

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
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

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
CODE_UNSUPPORTED = "clean_room_unsupported"
CODE_TOO_DEEP = "clean_room_too_deep"
CODE_TOO_LARGE = "clean_room_too_large"

#: A single string longer than this is refused before any rule runs. An
#: outbox line is capped far below it anyway; the bound keeps one oversized
#: value from stalling every push behind a slow scan.
MAX_STRING_CHARS = 64 * 1024

#: Nesting deeper than this is refused. A real residual is a few levels deep;
#: anything past this is either an accident or an attempt to make the walk
#: raise instead of refuse, and a cycle reaches it too.
MAX_DEPTH = 64


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
                d = re.escape(_fold(dom.strip().lstrip(".")))
                out.append((CODE_HOST, "internal_domains",
                            re.compile(rf"(?<![\w.-])(?:[\w-]+\.)*{d}(?![\w-])", re.I)))
            self._compiled["p"] = out
        return self._compiled["p"]


# Look-alike letters from other scripts that render as Latin ones. NFKC does
# not fold these (they are distinct letters, not compatibility forms), so a
# Cyrillic "е" inside a listed name would otherwise walk straight past it.
# Applied after casefold, so only lower-case forms are needed.
_CONFUSABLES = str.maketrans({
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o",
    "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "ѕ": "s", "і": "i", "ї": "i",
    "ј": "j", "ԁ": "d", "ԛ": "q", "ԝ": "w", "ɡ": "g", "ⅼ": "l", "ı": "i",
    "α": "a", "β": "b", "ε": "e", "η": "n", "ι": "i", "κ": "k", "μ": "u", "ν": "v",
    "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "χ": "x", "ω": "w",
})


def _strip_format(text: str) -> str:
    """NFKC, then drop format characters (zero-width spaces, soft hyphens,
    direction marks). Case is kept: secret shapes are case-sensitive."""
    t = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in t if unicodedata.category(ch) != "Cf")


def _fold(text: str) -> str:
    """What list terms are matched against: NFKD with combining marks,
    variation selectors and format characters removed, case-folded, and with
    cross-script look-alikes mapped to Latin. ("café" folds to "cafe", so
    accented text is compared on its base letters.)"""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(ch for ch in t if unicodedata.category(ch) not in ("Mn", "Me", "Cf"))
    return t.casefold().translate(_CONFUSABLES)


# A term boundary is anything that is not a letter or digit. `_` counts as a
# boundary on purpose: identifiers (`name_prod`, `PROD_NAME`) are the likeliest
# shape a name leaks in.
_B_BEFORE = r"(?<![^\W_])"
_B_AFTER = r"(?![^\W_])"


def _term_pattern(term: str) -> re.Pattern:
    words = [re.escape(w) for w in _fold(term).split()]
    if not words:
        raise CleanRoomUnavailable("clean-room list contains an empty term")
    return re.compile(_B_BEFORE + r"\s+".join(words) + _B_AFTER)


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
    # Bounded on purpose: the unbounded form was quadratic ("a@" + "a-" * n).
    ("url_userinfo", re.compile(r"\b[a-z][a-z0-9+.-]{0,31}://[^\s/:@]{1,256}:[^\s/@]{1,256}@", re.I)),
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


def _scan_text(text: str, path: str, lists: Lists, hits: list[Hit]) -> None:
    plain = _strip_format(text)
    for rule, rx in _SECRET_RULES:
        for m in rx.finditer(plain):
            hits.append(Hit(CODE_SECRET, rule, path, _mask(m.group(0))))
    for m in _EMAIL.finditer(plain):
        hits.append(Hit(CODE_EMAIL, "email", path, _mask(m.group(0))))
    for m in _IPV4.finditer(plain):
        try:
            ip = ipaddress.IPv4Address(m.group(0))
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            hits.append(Hit(CODE_HOST, "private_ip", path, _mask(m.group(0))))
    folded = _fold(text)
    for code, rule, rx in lists.patterns():
        for m in rx.finditer(folded):
            hits.append(Hit(code, rule, path, _mask(m.group(0))))


def _walk(obj: Any, path: str, depth: int, lists: Lists, hits: list[Hit]) -> None:
    if depth > MAX_DEPTH:
        hits.append(Hit(CODE_TOO_DEEP, "max_depth", path, f"depth>{MAX_DEPTH}"))
        return
    if isinstance(obj, str):
        if len(obj) > MAX_STRING_CHARS:
            hits.append(Hit(CODE_TOO_LARGE, "max_string", path, f"{len(obj)}ch"))
            return
        _scan_text(obj, path, lists, hits)
    elif obj is None or isinstance(obj, (bool, int, float)):
        return
    elif isinstance(obj, dict):
        for i, (k, v) in enumerate(obj.items()):
            if not isinstance(k, str):
                hits.append(Hit(CODE_UNSUPPORTED, "non_string_key", f"{path}{{#{i}}}", type(k).__name__))
                continue
            before = len(hits)
            _scan_text(k, f"{path}{{#{i}}}<key>", lists, hits)
            # A key is rendered into child paths only when it is itself clean
            # and a plain identifier; otherwise its position stands in for it,
            # so a refusal never repeats what it caught.
            if len(hits) == before and re.fullmatch(r"\w{1,64}", k):
                kp = f"{path}.{k}"
            else:
                kp = f"{path}{{#{i}}}"
            _walk(v, kp, depth + 1, lists, hits)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _walk(v, f"{path}[{i}]", depth + 1, lists, hits)
    else:
        # bytes, sets and arbitrary objects cannot be judged, so they cannot leave.
        hits.append(Hit(CODE_UNSUPPORTED, "non_json_leaf", path, type(obj).__name__))


def check(payload: Any, lists: Lists) -> list[Hit]:
    """Every hit in ``payload``, in document order. Empty means it may leave.

    Only JSON-shaped data is judged: str, int, float, bool, None, lists and
    dicts with string keys. Anything else is a hit, as is nesting past
    MAX_DEPTH (which a cycle always reaches).
    """
    hits: list[Hit] = []
    _walk(payload, "$", 0, lists, hits)
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
    terms = tuple(t.strip() for t in v)
    if any(not _fold(t).strip() for t in terms):
        # A blank term is a typo, not a decision; refusing it at load is
        # cheaper than discovering later that a line was meant to say something.
        raise CleanRoomUnavailable(f"{LISTS_NAME}: `{key}` contains a blank term")
    return terms


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
