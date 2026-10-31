"""The person's partition: signed answers to the hub's human gates (hub ADR 0008).

What would fail if the partition were dishonest: bytes that differ from the
hub's published vectors (every signature would fail, or worse, verify over
something else); a query the hub signs but this side does not; a key written
world-readable; a second key silently replacing the first; an envelope sent
that the ledger does not hold; a "confirm" that takes its verdict from the
hub instead of from what this person signed; confirming an answer this
partition never sent. Synthetic people and hubs only.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: E402

from agentco_harness import hub_client, partition  # noqa: E402
from agentco_harness.hub_client import HubRefusal  # noqa: E402

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "envelope-vectors.json").read_text())
HUB_ID = "0123456789abcdef0123456789abcdef"


# --------------------------------------------------------------------------- #
# the contract, against the hub's own bytes
# --------------------------------------------------------------------------- #


def test_signed_bytes_and_the_ed25519_signature_match_the_hubs_vectors():
    vector = next(v for v in VECTORS["vectors"] if v["alg"] == "ed25519")
    key = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(VECTORS["ed25519PrivateSeedHex"]))
    payload = vector["payload"]
    assert partition.signed_bytes(payload).decode() == vector["signedBytes"]
    assert partition.sign(key, payload) == vector["signature"], "Ed25519 is deterministic: same bytes, same signature"
    assert partition.fingerprint(key) == vector["fingerprint"]
    assert partition.key_id(key) == payload["key_id"]


def test_canonical_json_matches_the_spec_on_the_hard_cases():
    assert partition.canonical_json({"b": 1, "a": [True, None], "é": "\n\x01"}) == \
        '{"a":[true,null],"b":1,"é":"\\n\\u0001"}'.encode()
    with pytest.raises(partition.PartitionError):
        partition.canonical_json({"x": 1.5})


@pytest.mark.parametrize("method, path, body, query, expected", [
    ("GET", "/work", b"", "ready=1", "5b1fc75da4869179508275af473ed873f60d15e6cbe8e7b4b799db1e90445f04"),
    ("GET", "/events", b"", "since=abc&kind=Human Key",
     "6b2bc79f87eac4094554052cf248f0b8cab1a93584ad90959839b3fc85dd5185"),
    ("POST", "/answers", b'{"a":1}', None, "63c731a95fa1417345b021bf223057882d2392e3ec74aa93cadf3618f0911306"),
])
def test_request_signing_matches_the_hub_including_the_query(method, path, body, query, expected):
    """Vectors computed by the hub's `agentco.auth.sign`. A query the hub signs
    and this side did not would make every `GET /work?ready=1` unauthenticated."""
    assert hub_client.sign("s3cret", method, path, "1793318400", body, query) == expected


# --------------------------------------------------------------------------- #
# a hub that checks what a real one checks
# --------------------------------------------------------------------------- #


class FakeHub(hub_client.HubClient):
    """Answers the routes a partition uses, and verifies every envelope's
    signature against the public key it was given, as the hub would."""

    def __init__(self, person="dana"):
        super().__init__(url="https://hub.example", actor="relay-dana", secret="x")
        self.person, self.public = person, None
        self.items = {"w-1": {"id": "w-1", "title": "ship the rollback", "status": "awaiting_verify",
                              "verify_failures": 0,
                              "verify": {"kind": "human", "verifier": person, "check": "rollback exercised"}},
                      "w-2": {"id": "w-2", "title": "someone else's", "status": "awaiting_verify",
                              "verify_failures": 0, "verify": {"kind": "human", "verifier": "eli", "check": "c"}}}
        self.vehicles = [{"id": "v-1", "metadata": {"verifies": "w-1"}},
                         {"id": "v-2", "metadata": {"verifies": "w-2"}}]
        self.answers, self.held_list, self.refuse_next = [], [], None
        self.calls = []

    def _request(self, method, path, payload=None, query=None):
        self.calls.append((method, path, query))
        if (method, path) == ("GET", "/hub"):
            return {"hubId": HUB_ID, "label": "acme-hub"}
        if (method, path, query) == ("GET", "/work", "ready=1"):
            return {"items": self.vehicles}
        if method == "GET" and path.startswith("/work/"):
            return {"item": self.items[path.rsplit("/", 1)[1]]}
        if (method, path) == ("GET", "/answers/held"):
            return {"held": self.held_list}
        if (method, path) == ("POST", "/answers"):
            body = json.loads(payload["envelope"])
            self.public.verify(base64.b64decode(payload["signature"]), partition.signed_bytes(body))
            if self.refuse_next:
                refusal, self.refuse_next = self.refuse_next, None
                raise refusal
            self.answers.append(body)
            return {"answer": "applied", "receipt": {"receipt": {"item": body["item"], "status": "done"}}}
        raise AssertionError(f"unexpected {method} {path} {query}")


@pytest.fixture()
def made(tmp_path):
    hub = FakeHub()
    out = partition.Partition.init(tmp_path / "acme", hub_url="https://hub.example", label="acme",
                                   person="dana", transport_actor="relay-dana", client=hub)
    part = partition.Partition(tmp_path / "acme", client=hub)
    hub.public = part._key.public_key()
    return part, hub, out


def test_init_pins_the_hub_and_keeps_the_key_private(made, tmp_path):
    part, hub, out = made
    assert part.state.hub_id == HUB_ID == out["hubId"]
    assert out["fingerprint"].startswith(out["keyId"])
    mode = stat.S_IMODE(os.stat(tmp_path / "acme" / partition.KEY_FILE).st_mode)
    assert mode == 0o600, oct(mode)
    assert stat.S_IMODE(os.stat(tmp_path / "acme").st_mode) == 0o700
    assert "PRIVATE" not in out["publicKeyPem"]
    with pytest.raises(partition.PartitionError):
        partition.Partition.init(tmp_path / "acme", hub_url="x", label="acme", person="dana",
                                 transport_actor="relay-dana", client=hub)


def test_init_refuses_a_hub_with_no_generated_id(tmp_path):
    hub = FakeHub()
    hub._request = lambda *a, **k: {"hubId": None, "label": "local"}
    with pytest.raises(partition.PartitionError):
        partition.Partition.init(tmp_path / "p", hub_url="x", label="l", person="dana",
                                 transport_actor="relay-dana", client=hub)


def test_gates_lists_only_this_persons_human_gates(made):
    part, hub, _ = made
    assert [g["item"] for g in part.gates()] == ["w-1"]
    assert ("GET", "/work", "ready=1") in hub.calls
    hub.items["w-1"]["status"] = "done"  # answered; its vehicle closes on the next routing pass
    assert part.gates() == []


def test_an_answer_is_signed_bound_and_ledgered_before_it_is_sent(made):
    part, hub, _ = made
    hub.items["w-1"]["verify_failures"] = 2
    result = part.answer("w-1", passed=True, reason="exercised on staging")
    assert result["answer"] == "applied"
    [sent] = hub.answers
    assert (sent["hub"], sent["item"], sent["verify_attempt"], sent["signer"], sent["epoch"]) == \
        (HUB_ID, "w-1", 2, "dana", 0)
    assert sent["key_id"] == part.state.key_id and len(sent["nonce"]) == 32
    ledger = part._ledger()
    assert ledger[0]["payload"] == sent and ledger[0]["digest"] == partition.digest(sent)
    assert ledger[1]["result"]["answer"] == "applied"


def test_a_refused_answer_is_ledgered_with_the_refusal(made):
    part, hub, _ = made
    hub.refuse_next = HubRefusal("stale", "verify_failures 1, you read 0")
    with pytest.raises(HubRefusal):
        part.answer("w-1", passed=False, reason="not yet")
    assert part._ledger()[-1]["result"] == {"refused": "stale", "message": "verify_failures 1, you read 0"}


def test_a_transferred_run_is_answered_again_at_the_epoch_the_hub_states(made):
    part, hub, _ = made
    hub.refuse_next = HubRefusal("not_the_owner", "an attestation was made at 0 (ownership epoch 2)")
    part.answer("w-1", passed=True, reason="ok")
    assert [a["epoch"] for a in hub.answers] == [2]
    assert hub.answers[0]["verdict"] == {"passed": True, "reason": "ok"}, "same verdict, new epoch"


def test_an_answer_needs_a_reason(made):
    part, _, _ = made
    with pytest.raises(partition.PartitionError):
        part.answer("w-1", passed=True, reason="  ")


# --------------------------------------------------------------------------- #
# held answers (S4)
# --------------------------------------------------------------------------- #


def test_held_answers_are_matched_to_the_ledger_and_strangers_are_flagged(made):
    part, hub, _ = made
    part.answer("w-1", passed=False, reason="typed on the plane")
    mine = part._ledger()[0]
    hub.held_list = [{"item": "w-1", "verifyAttempt": 0, "digest": mine["digest"], "nonce": "n1",
                      "message": "needs your validation"},
                     {"item": "w-1", "verifyAttempt": 0, "digest": "f" * 64, "nonce": "n2",
                      "message": "needs your validation"}]
    held = part.held()
    assert [h["mine"] for h in held] == [True, False]
    assert held[0]["verdict"] == {"passed": False, "reason": "typed on the plane"}
    assert held[1]["verdict"] is None and held[1]["message"] == "the hub reports an answer you did not send"


def test_confirm_signs_anew_with_the_verdict_from_the_ledger(made):
    part, hub, _ = made
    part.answer("w-1", passed=False, reason="typed on the plane")
    original = hub.answers[0]
    hub.answers.clear()
    hub.held_list = [{"item": "w-1", "verifyAttempt": 0, "digest": partition.digest(original), "nonce": "n1"}]
    part.confirm("w-1")
    [again] = hub.answers
    assert again["verdict"] == original["verdict"], "the verdict is this person's, from the ledger"
    assert again["nonce"] != original["nonce"], "a NEW envelope, never a resend"
    assert part._ledger()[-2]["confirms"] == partition.digest(original)


def test_confirm_refuses_an_answer_this_partition_never_sent(made):
    part, hub, _ = made
    hub.held_list = [{"item": "w-1", "verifyAttempt": 0, "digest": "f" * 64, "nonce": "n2"}]
    with pytest.raises(partition.PartitionError, match="never sent"):
        part.confirm("w-1")
    assert hub.answers == []


def test_a_partition_refuses_a_swapped_key(made, tmp_path):
    other = ed25519.Ed25519PrivateKey.generate()
    (tmp_path / "acme" / partition.KEY_FILE).write_bytes(other.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    with pytest.raises(partition.PartitionError, match="not the key"):
        partition.Partition(tmp_path / "acme", client=FakeHub())
