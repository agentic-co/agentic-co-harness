"""The mechanical half of the clean room (hub ADR 0015 §3).

Everything that leaves a partition for a hub's shared layer passes this check
first. It is a judge, not a rewriter: the model writes the residual, code
decides whether it may leave. Every name and secret below is fabricated — the
real lists are private per-partition configuration and never enter this repo.
"""

import json

import pytest

from agentco_harness import clean_room
from agentco_harness.clean_room import (
    CleanRoomRefused,
    CleanRoomUnavailable,
    Lists,
    assert_clean,
    check,
    list_path,
    load_lists,
)

# Fabricated, deliberately unlike any real organisation or person.
LISTS = Lists(
    internal_names=("Quillfeather", "Project Marigold"),
    never_leave=("tangerine-ledger",),
    people=("Orsolya Brannigan",),
    internal_domains=("intra.quillfeather.example",),
)


def write_lists(tmp_path, data=None, name=clean_room.LISTS_NAME):
    p = tmp_path / name
    p.write_text(json.dumps(data if data is not None else {
        "schemaVersion": 1,
        "internal_names": ["Quillfeather"],
        "never_leave": [],
        "people": [],
        "internal_domains": [],
    }))
    return p


def codes(hits):
    return sorted({h.code for h in hits})


# ---------------------------------------------------------------- clean input


def test_clean_payload_passes():
    payload = {
        "lesson": "A gate that refuses correct work 50% of the time should be split.",
        "tags": ["gates", "verification"],
        "n": 3,
    }
    assert check(payload, LISTS) == []
    assert_clean(payload, LISTS)  # does not raise


# ---------------------------------------------------------------- list rules


def test_internal_name_is_caught_case_insensitively_and_nested():
    payload = {"steps": [{"note": "deploy the QUILLFEATHER api first"}]}
    hits = check(payload, LISTS)
    assert codes(hits) == [clean_room.CODE_INTERNAL_NAME]
    assert hits[0].path == "$.steps[0].note"


def test_multiword_name_matches_across_whitespace():
    hits = check("context from project   marigold", LISTS)
    assert codes(hits) == [clean_room.CODE_INTERNAL_NAME]


def test_name_match_is_whole_word():
    # "Quillfeathered" is not the name; substring matches would make the
    # check refuse innocent English and train people to switch it off.
    assert check("a quillfeathered pen", LISTS) == []


def test_never_leave_and_people():
    hits = check({"a": "see tangerine-ledger", "b": "asked Orsolya Brannigan"}, LISTS)
    assert codes(hits) == [clean_room.CODE_NEVER_LEAVE, clean_room.CODE_PERSON]


def test_dict_keys_are_checked_too():
    hits = check({"Quillfeather": 1}, LISTS)
    assert codes(hits) == [clean_room.CODE_INTERNAL_NAME]


def test_internal_domain_and_subdomains():
    hits = check("pulled from https://build.intra.quillfeather.example/x", LISTS)
    assert clean_room.CODE_HOST in codes(hits)


# ---------------------------------------------------------------- built-in rules


@pytest.mark.parametrize("secret", [
    "AKIA" + "Q" * 16,
    "ghp_" + "a1" * 18,
    "github_pat_" + "B" * 30,
    "xoxb-" + "1234567890-" + "abcdefghij",
    "sk-ant-" + "x" * 40,
    "sk-" + "Z" * 40,
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlc2lnbmF0dXJl",
    "password = hunter2hunter2",
    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
])
def test_secret_shapes_are_caught(secret):
    hits = check({"text": f"the value was {secret} ok"}, LISTS)
    assert clean_room.CODE_SECRET in codes(hits), secret


def test_secret_never_echoed_in_hit_or_refusal():
    secret = "ghp_" + "SeCrEt" * 6
    with pytest.raises(CleanRoomRefused) as ei:
        assert_clean({"x": secret}, LISTS)
    rendered = str(ei.value) + json.dumps([h.as_dict() for h in ei.value.hits])
    assert secret not in rendered
    assert "SeCrEt" * 2 not in rendered


def test_email_is_caught():
    assert clean_room.CODE_EMAIL in codes(check("mail someone@example.org", LISTS))


def test_private_ip_is_caught_but_public_doc_ip_is_not():
    assert clean_room.CODE_HOST in codes(check("db at 10.2.3.4:5432", LISTS))  # leakguard: allow (planted fixture)
    assert clean_room.CODE_HOST in codes(check("db at 192.168.1.20", LISTS))  # leakguard: allow (planted fixture)
    # Only private ranges: a dotted version string is not a host.
    assert check("version 1.2.3.4 of the spec", LISTS) == []  # leakguard: allow (planted fixture)


# ---------------------------------------------------------------- refusal shape


def test_refusal_carries_code_and_every_hit():
    with pytest.raises(CleanRoomRefused) as ei:
        assert_clean({"a": "Quillfeather", "b": "x@example.com"}, LISTS)
    assert ei.value.code == clean_room.CODE_REFUSED
    assert codes(ei.value.hits) == [clean_room.CODE_EMAIL, clean_room.CODE_INTERNAL_NAME]


# ---------------------------------------------------------------- fail-closed


def test_missing_lists_refuse(tmp_path, monkeypatch):
    monkeypatch.delenv(clean_room.LISTS_ENV, raising=False)
    with pytest.raises(CleanRoomUnavailable) as ei:
        assert_clean({"a": "harmless"}, None, store_dir=tmp_path)
    assert ei.value.code == clean_room.CODE_UNAVAILABLE


def test_empty_internal_names_refuse(tmp_path):
    # A list file that names nothing is indistinguishable from a forgotten
    # one; passing it would make the check decorative.
    p = write_lists(tmp_path, {"schemaVersion": 1, "internal_names": []})
    with pytest.raises(CleanRoomUnavailable):
        load_lists(p)


def test_unknown_schema_refuses(tmp_path):
    p = write_lists(tmp_path, {"schemaVersion": 99, "internal_names": ["A"]})
    with pytest.raises(CleanRoomUnavailable):
        load_lists(p)


def test_malformed_json_refuses(tmp_path):
    p = tmp_path / clean_room.LISTS_NAME
    p.write_text("{not json")
    with pytest.raises(CleanRoomUnavailable):
        load_lists(p)


def test_resolution_order(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.json"
    monkeypatch.setenv(clean_room.LISTS_ENV, str(tmp_path / "env.json"))
    assert list_path(explicit, store_dir=tmp_path) == explicit
    assert list_path(None, store_dir=tmp_path) == tmp_path / "env.json"
    monkeypatch.delenv(clean_room.LISTS_ENV)
    assert list_path(None, store_dir=tmp_path) == tmp_path / clean_room.LISTS_NAME


def test_lists_loaded_from_store_dir(tmp_path, monkeypatch):
    monkeypatch.delenv(clean_room.LISTS_ENV, raising=False)
    write_lists(tmp_path)
    with pytest.raises(CleanRoomRefused):
        assert_clean("about quillfeather", None, store_dir=tmp_path)
    assert_clean("about nothing in particular", None, store_dir=tmp_path)
