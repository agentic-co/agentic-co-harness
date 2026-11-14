"""The push outbox: the harness's half of ADR 0013 (hub repo).

Every outbound statement is one JSON line in the hub's L1 outbox format; the
hub's drainer signs and publishes it. These tests pin: wire-format parity with
the hub, content-derived idempotency, the fail-closed clean-room gate that runs
BEFORE anything touches disk, and per-class opt-in.

Every name below is fabricated. The real clean-room lists are private
per-partition configuration and never enter this repository.
"""

from __future__ import annotations

import json
import re

import pytest
import yaml

from agentco_harness import clean_room, outbox
from agentco_harness.clean_room import CleanRoomRefused, CleanRoomUnavailable
from agentco_harness.config import Config
from agentco_harness.outbox import (
    LINE_FIELDS,
    PUSH_VERBS,
    Outbox,
    OutboxConfig,
    OutboxRefused,
    derive_line_id,
)

# The hub's wire contract, copied from its `agentco/outbox.py` (main). If the
# hub changes either tuple, this file is where the drift gets noticed.
HUB_LINE_FIELDS = ("line_id", "at", "verb", "payload", "agent_label")
HUB_PUSH_VERBS = (
    "claim_scope",
    "release_scope",
    "snapshot",
    "work_report",
    "attest",
    "adjudicate",
    "sop_revise",
    "sop_activate",
)

REPORT = {"item_id": "w-17", "outcome": "done", "summary": "Ran the nightly sweep."}


@pytest.fixture
def store(tmp_path):
    """A store dir holding a valid fabricated clean-room list."""
    (tmp_path / clean_room.LISTS_NAME).write_text(json.dumps({
        "schemaVersion": 1,
        "internal_names": ["Quillfeather"],
        "never_leave": ["tangerine-ledger"],
        "people": ["Orsolya Brannigan"],
        "internal_domains": ["intra.quillfeather.example"],
    }))
    return tmp_path


def make(store, **kw) -> Outbox:
    cfg = OutboxConfig(path=str(store / "outbox.jsonl"), **kw)
    return Outbox(cfg, store_dir=store)


def lines(box: Outbox) -> list[dict]:
    if not box.path.exists():
        return []
    return [json.loads(l) for l in box.path.read_text().splitlines() if l.strip()]


# ------------------------------------------------------------ format parity


def test_wire_contract_matches_the_hubs():
    assert LINE_FIELDS == HUB_LINE_FIELDS
    assert PUSH_VERBS == HUB_PUSH_VERBS


def test_written_line_has_exactly_the_hub_field_set(store):
    box = make(store)
    box.push("work_report", REPORT, agent_label="harness-a")

    (line,) = lines(box)
    assert tuple(sorted(line)) == tuple(sorted(HUB_LINE_FIELDS))
    assert line["verb"] == "work_report"
    assert line["payload"] == REPORT
    assert line["agent_label"] == "harness-a"
    assert re.fullmatch(r"ob_[0-9a-f]{16}", line["line_id"])
    assert line["at"].endswith("+00:00")  # ISO-8601, UTC, as the hub writes it


def test_missing_label_is_written_as_null_like_the_hub(store):
    box = make(store)
    box.push("work_report", REPORT)
    assert lines(box)[0]["agent_label"] is None


def test_each_line_is_compact_sorted_json_ending_in_newline(store):
    box = make(store)
    box.push("work_report", REPORT)
    raw = box.path.read_bytes()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    parsed = json.loads(raw)
    assert raw.decode().rstrip("\n") == json.dumps(parsed, sort_keys=True)


def test_expected_version_rides_in_the_payload_untouched(store):
    """ADR 0013 s4: the drainer re-reads on a version conflict; the harness
    only has to be able to say which version it saw."""
    box = make(store)
    payload = {**REPORT, "expected_version": "v-41"}
    box.push("work_report", payload)
    assert lines(box)[0]["payload"]["expected_version"] == "v-41"


# ---------------------------------------------------- deterministic line_id


def test_line_id_is_content_derived_and_stable():
    a = derive_line_id("work_report", REPORT)
    assert a == derive_line_id("work_report", dict(reversed(list(REPORT.items()))))
    assert re.fullmatch(r"ob_[0-9a-f]{16}", a)
    assert a != derive_line_id("work_report", {**REPORT, "outcome": "failed"})
    assert a != derive_line_id("attest", REPORT)


def test_attempt_id_is_part_of_the_identity():
    a = derive_line_id("work_report", REPORT, attempt_id="att-1")
    assert a != derive_line_id("work_report", REPORT, attempt_id="att-2")
    assert a != derive_line_id("work_report", REPORT)
    assert derive_line_id("work_report", REPORT, attempt_id=None) == derive_line_id(
        "work_report", REPORT, attempt_id=""
    )


def test_line_id_is_the_documented_sha256_prefix():
    import hashlib

    canon = json.dumps(["work_report", REPORT, ""], sort_keys=True, separators=(",", ":"))
    want = "ob_" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
    assert derive_line_id("work_report", REPORT) == want


def test_replay_is_a_no_op(store):
    box = make(store)
    first = box.push("work_report", REPORT, attempt_id="att-1")
    again = box.push("work_report", REPORT, attempt_id="att-1")

    assert first.written is True and again.written is False
    assert first.line["line_id"] == again.line["line_id"]
    assert len(lines(box)) == 1


def test_replay_dedupes_across_instances_and_after_a_torn_tail(store):
    make(store).push("work_report", REPORT)
    # A crash left half a line at the end of the file.
    with open(store / "outbox.jsonl", "ab") as fh:
        fh.write(b'{"line_id": "ob_tor')

    box = make(store)
    assert box.push("work_report", REPORT).written is False
    fresh = box.push("work_report", {**REPORT, "item_id": "w-18"})
    assert fresh.written is True

    raw = (store / "outbox.jsonl").read_bytes().split(b"\n")
    # the torn fragment stays its own line; the new record lands whole after it
    assert any(l.startswith(b'{"line_id": "ob_tor') for l in raw)
    assert json.loads(raw[-2])["payload"]["item_id"] == "w-18"


def test_distinct_content_appends_distinct_lines(store):
    box = make(store)
    box.push("work_report", REPORT)
    box.push("work_report", {**REPORT, "item_id": "w-18"})
    assert len(lines(box)) == 2


def test_concurrent_pushers_never_duplicate_or_tear(store):
    import threading

    box = make(store)
    errs: list[BaseException] = []

    def go(i: int) -> None:
        try:
            for _ in range(3):  # every thread pushes the same 3 contents
                for j in range(3):
                    make(store).push("work_report", {**REPORT, "item_id": f"w-{j}"})
        except BaseException as e:  # pragma: no cover
            errs.append(e)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]

    assert not errs
    assert sorted(l["payload"]["item_id"] for l in lines(box)) == ["w-0", "w-1", "w-2"]


# ------------------------------------------------------ hub validation parity


def test_payload_with_actor_is_refused(store):
    box = make(store)
    with pytest.raises(OutboxRefused) as ei:
        box.push("work_report", {**REPORT, "actor": "someone-else"})
    assert ei.value.code == outbox.CODE_INVALID
    assert lines(box) == []


def test_unknown_verb_is_refused(store):
    box = make(store)
    for verb in ("work_create", "", None, "WORK_REPORT"):
        with pytest.raises(OutboxRefused) as ei:
            box.push(verb, REPORT)
        assert ei.value.code == outbox.CODE_INVALID
    assert lines(box) == []


def test_non_dict_payload_and_bad_label_are_refused(store):
    box = make(store)
    with pytest.raises(OutboxRefused):
        box.push("work_report", ["not", "a", "dict"])
    with pytest.raises(OutboxRefused):
        box.push("work_report", REPORT, agent_label=42)
    assert lines(box) == []


def test_oversize_line_is_refused(store):
    box = make(store)
    with pytest.raises(OutboxRefused) as ei:
        box.push("work_report", {"body": "x" * (outbox.MAX_LINE_BYTES + 1)})
    assert ei.value.code == outbox.CODE_INVALID
    assert lines(box) == []


# ------------------------------------------------------------- clean room


def test_clean_room_hit_writes_nothing(store):
    box = make(store)
    with pytest.raises(CleanRoomRefused) as ei:
        box.push("work_report", {**REPORT, "summary": "Fixed the Quillfeather importer."})
    assert ei.value.hits[0].code == clean_room.CODE_INTERNAL_NAME
    assert not box.path.exists()


def test_secret_in_payload_writes_nothing(store):
    box = make(store)
    with pytest.raises(CleanRoomRefused):
        box.push("work_report", {**REPORT, "summary": "key " + "AKIA" + "ABCDEFGHIJKLMNOP" + " leaked"})  # assembled: no literal key in source
    assert not box.path.exists()


def test_agent_label_is_checked_too(store):
    """The label is written to disk; a self-reported name is still a leak vector."""
    box = make(store)
    with pytest.raises(CleanRoomRefused):
        box.push("work_report", REPORT, agent_label="Orsolya Brannigan's laptop")
    assert not box.path.exists()


def test_missing_lists_file_writes_nothing(tmp_path):
    box = make(tmp_path)  # no clean-room.json beside the store
    with pytest.raises(CleanRoomUnavailable):
        box.push("work_report", REPORT)
    assert not box.path.exists()


def test_empty_internal_names_writes_nothing(tmp_path):
    (tmp_path / clean_room.LISTS_NAME).write_text(
        json.dumps({"schemaVersion": 1, "internal_names": []})
    )
    box = make(tmp_path)
    with pytest.raises(CleanRoomUnavailable):
        box.push("work_report", REPORT)
    assert not box.path.exists()


def test_clean_room_runs_before_the_file_is_created(store):
    """No stray empty file, no lock sidecar left behind by a refused push."""
    box = make(store)
    with pytest.raises(CleanRoomRefused):
        box.push("work_report", {**REPORT, "summary": "see tangerine-ledger"})
    assert sorted(p.name for p in store.iterdir()) == [clean_room.LISTS_NAME]


def test_explicit_clean_room_path_is_honoured(tmp_path):
    lists = tmp_path / "elsewhere" / "lists.json"
    lists.parent.mkdir()
    lists.write_text(json.dumps({"schemaVersion": 1, "internal_names": ["Quillfeather"]}))
    box = make(tmp_path, clean_room_path=str(lists))
    box.push("work_report", REPORT)
    assert len(lines(box)) == 1


def test_invalid_line_is_refused_before_the_clean_room_is_consulted(tmp_path):
    """Shape errors are the caller's bug and must not depend on list state."""
    box = make(tmp_path)  # lists missing
    with pytest.raises(OutboxRefused):
        box.push("nope", REPORT)


# ----------------------------------------------------- opt-in per verb class


def test_work_verbs_are_allowed_by_default(store):
    box = make(store)
    box.push("work_report", REPORT)
    box.push("attest", {"item_id": "w-17", "verdict": "pass"})
    assert [l["verb"] for l in lines(box)] == ["work_report", "attest"]


@pytest.mark.parametrize("verb", ["sop_revise"])
def test_knowledge_verbs_are_refused_unless_enabled(store, verb):
    box = make(store)
    with pytest.raises(OutboxRefused) as ei:
        box.push(verb, {"sop_id": "s-1", "note": "split the gate"})
    assert ei.value.code == outbox.CODE_CLASS_DISABLED
    assert "knowledge" in str(ei.value)
    assert lines(box) == []


def test_knowledge_verbs_pass_when_the_class_is_enabled(store):
    box = make(store, enabled_classes=["work", "knowledge"])
    box.push("sop_revise", {"sop_id": "s-1", "note": "split the gate"})
    assert lines(box)[0]["verb"] == "sop_revise"


def test_knowledge_only_config_refuses_work_verbs(store):
    box = make(store, enabled_classes=["knowledge"])
    with pytest.raises(OutboxRefused) as ei:
        box.push("work_report", REPORT)
    assert ei.value.code == outbox.CODE_CLASS_DISABLED


def test_disabled_class_is_refused_before_the_clean_room(tmp_path):
    box = make(tmp_path)  # lists missing AND class off: the cheaper gate wins
    with pytest.raises(OutboxRefused):
        box.push("sop_revise", {"sop_id": "s-1"})


def test_every_hub_verb_has_a_class():
    assert set(outbox.VERB_CLASS) == set(HUB_PUSH_VERBS)
    assert set(outbox.VERB_CLASS.values()) <= set(outbox.CLASSES) | {outbox.CLASS_NEVER}


@pytest.mark.parametrize("enabled", [["work"], ["work", "knowledge"], ["knowledge"]])
def test_sop_activate_is_never_sent_whatever_is_enabled(store, enabled):
    # Hub ADR 0015 s1: activation is the owner's act; the harness never sends it.
    box = make(store, enabled_classes=enabled)
    with pytest.raises(OutboxRefused) as ei:
        box.push("sop_activate", {"sopId": "s-1", "version": 2})
    assert ei.value.code == outbox.CODE_VERB_NEVER
    assert lines(box) == []


def test_the_never_class_cannot_be_enabled():
    assert outbox.CLASS_NEVER not in outbox.CLASSES


def test_unclassified_future_verb_is_refused_not_defaulted_to_work(store, monkeypatch):
    """A new hub verb must be classified here before it can ship."""
    monkeypatch.setattr(outbox, "PUSH_VERBS", (*outbox.PUSH_VERBS, "memory_submit"))
    with pytest.raises(OutboxRefused) as ei:
        make(store).push("memory_submit", {"k": "v"})
    assert ei.value.code == outbox.CODE_CLASS_DISABLED


# ---------------------------------------------------------------- config


def test_outbox_config_defaults():
    cfg = OutboxConfig()
    assert cfg.enabled_classes == ["work"]
    assert cfg.path == ".agentco/outbox.jsonl"
    assert cfg.clean_room_path is None


def test_config_loads_outbox_section_and_resolves_paths(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({
        "tasks_path": "tasks.jsonl",
        "outbox": {"enabled_classes": ["work", "knowledge"], "clean_room_path": "lists.json"},
    }))
    config = Config.load(cfg_file)
    assert config.outbox.enabled_classes == ["work", "knowledge"]
    assert config.outbox.path == str((tmp_path / ".agentco" / "outbox.jsonl").resolve())
    assert config.outbox.clean_room_path == str((tmp_path / "lists.json").resolve())


def test_config_without_outbox_section_gets_defaults(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({"tasks_path": "tasks.jsonl"}))
    assert Config.load(cfg_file).outbox.enabled_classes == ["work"]


def test_outbox_key_is_known_and_unknown_nested_key_warns(tmp_path, capsys):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({"outbox": {"enabled_classes": ["work"], "mystery": 1}}))
    Config.load(cfg_file)
    err = capsys.readouterr().err
    assert "unknown top-level key" not in err
    assert "mystery" in err


def test_unknown_class_name_is_an_error_not_a_silent_off(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({"outbox": {"enabled_classes": ["work", "memories"]}}))
    with pytest.raises(ValueError, match="memories"):
        Config.load(cfg_file)
