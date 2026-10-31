"""The person's partition: answering human gates with a signed envelope (hub ADR 0008).

One partition per (person, company hub). It holds the person's private key for
that hub and nothing else of theirs; it pins the hub's generated id; it keeps
every envelope it ever signed; and it talks to the hub only through a
transport credential that can reach the answer routes and the person's own
gates, never the queue at large. The hub cannot write the person's verdict:
it holds only the public half (`agentco human-key enrol`, on the hub's side).

Files, under the partition's directory (mode 0700, the key 0600):

    partition.json   {hub_url, label, person, transport_actor, hub_id, key_id}
    key.pem          the Ed25519 private key — never leaves this directory
    envelopes.jsonl  every envelope signed here, with what the hub answered

**Why the ledger matters (S4).** An answer that reaches the hub late is held,
not applied. Confirming it signs a NEW envelope whose verdict comes from this
ledger, matched by digest — never from the hub, which does not send verdicts
back. A held answer with no matching entry is one this partition never sent,
and is shown as such with no confirm.

**The contract** (`asop.answer/1`) is the hub's `docs/envelope.md`. The
canonical JSON and the signed bytes are re-implemented here because the
installed asop-spec predates its `canonical_json`; `tests/test_partition.py`
checks them byte for byte against the hub's published vectors
(`tests/fixtures/envelope-vectors.json`), so a drift fails CI rather than a
signature.

Needs the `partition` extra (`cryptography`).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from .hub_client import HubClient, HubRefusal

TYPE = "asop.answer/1"
STATE_FILE, KEY_FILE, LEDGER_FILE = "partition.json", "key.pem", "envelopes.jsonl"
_EPOCH_IN_MESSAGE = re.compile(r"\(ownership epoch (\d+)\)")


class PartitionError(Exception):
    """A partition-side refusal: something to fix here before the hub is asked."""


# --------------------------------------------------------------------------- #
# the contract: canonical bytes, digest, key identity
# --------------------------------------------------------------------------- #


def canonical_json(value: Any) -> bytes:
    """ASOP.md §3.10: keys by code point, no whitespace, UTF-8 unescaped except
    `"`, `\\` and controls, integers only. The same function asop-spec 0.6
    exports as `asop.manifest.canonical_json`."""
    def check(v: Any) -> None:
        if isinstance(v, float):
            raise PartitionError("an envelope may not contain a float")
        if isinstance(v, dict):
            for k, x in v.items():
                if not isinstance(k, str):
                    raise PartitionError("an envelope's keys are strings")
                check(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                check(x)
    check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def signed_bytes(payload: dict) -> bytes:
    """The type line, one newline, the canonical JSON (hub ADR 0008 N5)."""
    return TYPE.encode("ascii") + b"\n" + canonical_json(payload)


def digest(payload: dict) -> str:
    """What the hub's held list names an envelope by."""
    return hashlib.sha256(signed_bytes(payload)).hexdigest()


def _crypto():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as exc:  # pragma: no cover - only without the extra
        raise PartitionError("the partition needs `cryptography`: pip install 'agentco-harness[partition]'") from exc
    return serialization, ed25519


def public_der(private_key) -> bytes:
    serialization, _ = _crypto()
    return private_key.public_key().public_bytes(serialization.Encoding.DER,
                                                 serialization.PublicFormat.SubjectPublicKeyInfo)


def fingerprint(private_key) -> str:
    """SHA-256 of the DER SubjectPublicKeyInfo — what the operator types."""
    return hashlib.sha256(public_der(private_key)).hexdigest()


def key_id(private_key) -> str:
    return fingerprint(private_key)[:32]


def sign(private_key, payload: dict) -> str:
    return base64.b64encode(private_key.sign(signed_bytes(payload))).decode("ascii")


# --------------------------------------------------------------------------- #
# the partition
# --------------------------------------------------------------------------- #


@dataclass
class State:
    hub_url: str
    label: str
    person: str
    transport_actor: str
    hub_id: str
    key_id: str


class Partition:
    """One person's partition for one company hub, rooted at `directory`.

    `secret` is the transport credential's HMAC secret. It is never written
    here: pass it, or set `AGENTCO_PARTITION_SECRET`, at each use.
    """

    def __init__(self, directory: Path | str, *, secret: Optional[str] = None,
                 client: Optional[HubClient] = None):
        self.dir = Path(directory).expanduser()
        state_path = self.dir / STATE_FILE
        if not state_path.exists():
            raise PartitionError(f"no partition at {self.dir}: run `agentic-co partition init` first")
        self.state = State(**json.loads(state_path.read_text()))
        serialization, _ = _crypto()
        self._key = serialization.load_pem_private_key((self.dir / KEY_FILE).read_bytes(), password=None)
        if key_id(self._key) != self.state.key_id:
            raise PartitionError("key.pem is not the key this partition was set up with")
        secret = secret or os.environ.get("AGENTCO_PARTITION_SECRET")
        self.client = client or HubClient(url=self.state.hub_url, actor=self.state.transport_actor,
                                          secret=secret or "")

    # -- setup ---------------------------------------------------------------

    @classmethod
    def init(cls, directory: Path | str, *, hub_url: str, label: str, person: str,
             transport_actor: str, secret: Optional[str] = None,
             client: Optional[HubClient] = None) -> dict:
        """Generate this partition's key, pin the hub's id, and say what to
        hand the hub's operator. Refuses to overwrite an existing partition:
        a second key for the same hub is a new enrolment, on purpose."""
        _, ed25519 = _crypto()
        serialization, _ = _crypto()
        root = Path(directory).expanduser()
        if (root / STATE_FILE).exists():
            raise PartitionError(f"{root} already holds a partition; use a new directory for a new key")
        client = client or HubClient(url=hub_url, actor=transport_actor,
                                     secret=secret or os.environ.get("AGENTCO_PARTITION_SECRET", ""))
        hub = client._request("GET", "/hub")
        hub_id = hub.get("hubId")
        if not isinstance(hub_id, str) or not re.fullmatch(r"[0-9a-f]{32}", hub_id):
            raise PartitionError(f"the hub at {hub_url} reported no generated id")
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        private = ed25519.Ed25519PrivateKey.generate()
        key_path = root / KEY_FILE
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        state = State(hub_url=hub_url, label=label, person=person, transport_actor=transport_actor,
                      hub_id=hub_id, key_id=key_id(private))
        (root / STATE_FILE).write_text(json.dumps(asdict(state), indent=2) + "\n")
        public_pem = private.public_key().public_bytes(serialization.Encoding.PEM,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo)
        return {"hubId": hub_id, "hubLabel": hub.get("label"), "keyId": state.key_id,
                "fingerprint": fingerprint(private), "publicKeyPem": public_pem.decode("ascii")}

    # -- the ledger ------------------------------------------------------------

    def _ledger(self) -> list[dict]:
        path = self.dir / LEDGER_FILE
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def _record(self, entry: dict) -> None:
        with open(self.dir / LEDGER_FILE, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    # -- reading the hub ---------------------------------------------------------

    def gates(self) -> list[dict]:
        """The gates waiting on this person: each ready vehicle assigned to the
        party, with the item it verifies read fresh. An item already answered
        is left out even while its vehicle is still open: the hub closes a moot
        vehicle on its next routing pass, not at the answer."""
        ready = self.client._request("GET", "/work", query="ready=1").get("items") or []
        out = []
        for vehicle in ready:
            target = (vehicle.get("metadata") or {}).get("verifies")
            if not target:
                continue
            item = self.client._request("GET", f"/work/{target}")["item"]
            gate = item.get("verify") or {}
            if (gate.get("kind") != "human" or gate.get("verifier") != self.state.person
                    or item.get("status") not in ("awaiting_verify", "verify_failed")):
                continue
            out.append({"item": item["id"], "title": item.get("title"), "check": gate.get("check"),
                        "status": item.get("status"), "verifyAttempt": item.get("verify_failures", 0)})
        return out

    # -- answering -------------------------------------------------------------

    def _payload(self, item_id: str, verdict: dict, choice: Optional[str], *, epoch: int = 0,
                 stage: Optional[int] = None) -> dict:
        item = self.client._request("GET", f"/work/{item_id}")["item"]
        return {
            "type": TYPE, "hub": self.state.hub_id, "item": item_id,
            "verify_attempt": int(item.get("verify_failures", 0)), "stage": stage, "epoch": epoch,
            "verdict": verdict, "choice": choice, "signer": self.state.person,
            "key_id": self.state.key_id, "nonce": secrets.token_hex(16), "issued_at": int(time.time()),
        }

    def _send(self, payload: dict, *, confirms: Optional[str] = None) -> dict:
        signature = sign(self._key, payload)
        entry = {"payload": payload, "signature": signature, "digest": digest(payload),
                 "signedAt": payload["issued_at"], "confirms": confirms}
        self._record(entry)  # before sending: an envelope signed is an envelope kept
        try:
            answer = self.client._request("POST", "/answers",
                                          {"envelope": json.dumps(payload), "signature": signature})
        except HubRefusal as refusal:
            self._record({"digest": entry["digest"], "result": {"refused": refusal.code,
                                                                "message": refusal.message}})
            raise
        self._record({"digest": entry["digest"], "result": {"answer": answer.get("answer"),
                                                            "receipt": answer.get("receipt")}})
        return answer

    def answer(self, item_id: str, *, passed: bool, reason: str, choice: Optional[str] = None) -> dict:
        """Sign and send this person's verdict on `item_id`'s gate. A run's
        transfer moves its epoch; the hub's refusal states the current one,
        and the same verdict is signed once more at that epoch."""
        if not isinstance(reason, str) or not reason.strip():
            raise PartitionError("say why: the reason is part of what you sign")
        verdict = {"passed": bool(passed), "reason": reason.strip()}
        try:
            return self._send(self._payload(item_id, verdict, choice))
        except HubRefusal as refusal:
            found = _EPOCH_IN_MESSAGE.search(refusal.message) if refusal.code == "not_the_owner" else None
            if not found:
                raise
            return self._send(self._payload(item_id, verdict, choice, epoch=int(found.group(1))))

    # -- held answers (S4) -------------------------------------------------------

    def held(self) -> list[dict]:
        """The hub's held answers for this person, each matched against the
        ledger by digest. `mine: False` is an answer this partition never
        signed — shown, never confirmable."""
        listed = self.client._request("GET", "/answers/held").get("held") or []
        signed = {e["digest"]: e for e in self._ledger() if "payload" in e}
        out = []
        for entry in listed:
            own = signed.get(entry.get("digest"))
            out.append({**entry, "mine": own is not None,
                        "verdict": own["payload"]["verdict"] if own else None,
                        "message": entry.get("message") if own
                        else "the hub reports an answer you did not send"})
        return out

    def confirm(self, item_id: str) -> dict:
        """Confirm a held answer: a NEW envelope, fresh, with the verdict taken
        from this partition's own record of the one being held."""
        candidates = [h for h in self.held() if h["item"] == item_id]
        if not candidates:
            raise PartitionError(f"no held answer for {item_id}")
        mine = [h for h in candidates if h["mine"]]
        if not mine:
            raise PartitionError(f"the hub holds an answer for {item_id} that this partition never sent; "
                                 "nothing to confirm — tell the hub's operator")
        held = mine[-1]
        original = next(e for e in self._ledger() if e.get("digest") == held["digest"] and "payload" in e)
        before = original["payload"]
        return self._send(self._payload(item_id, before["verdict"], before["choice"], epoch=before["epoch"],
                                        stage=before["stage"]), confirms=held["digest"])


# --------------------------------------------------------------------------- #
# CLI: `agentic-co partition ...`
# --------------------------------------------------------------------------- #

import click  # noqa: E402  (the module above is importable without the CLI)


def _open(directory: str) -> Partition:
    try:
        return Partition(directory)
    except PartitionError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group("partition")
def partition_group() -> None:
    """Answer the hub's human gates as yourself, with a signed envelope.

    The transport secret is read from AGENTCO_PARTITION_SECRET, never stored."""


@partition_group.command("init")
@click.argument("directory")
@click.option("--hub", "hub_url", required=True, help="the company hub's URL")
@click.option("--label", required=True, help="a name for this hub, for you")
@click.option("--person", required=True, help="you, exactly as the hub declares you in ASOP_HUMANS")
@click.option("--transport", "transport_actor", required=True,
              help="the transport credential's actor name the hub's operator gave you")
def partition_init(directory, hub_url, label, person, transport_actor):
    """Create a key for one hub and pin its id. Prints what to hand the operator."""
    try:
        out = Partition.init(directory, hub_url=hub_url, label=label, person=person,
                             transport_actor=transport_actor)
    except (PartitionError, HubRefusal) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"hub {out['hubLabel']} ({out['hubId']}) pinned; key {out['keyId']} created.")
    click.echo("\nHand this PUBLIC key to the hub's operator:\n")
    click.echo(out["publicKeyPem"])
    click.echo("Then read them this fingerprint on a DIFFERENT channel (call, in person):\n")
    click.echo(f"  {out['fingerprint']}")


@partition_group.command("gates")
@click.argument("directory")
@click.option("--json", "as_json", is_flag=True)
def partition_gates(directory, as_json):
    """The gates waiting on you."""
    gates = _open(directory).gates()
    if as_json:
        click.echo(json.dumps(gates, indent=2))
        return
    for g in gates or []:
        click.echo(f"{g['item']}  attempt {g['verifyAttempt']}  {g['title']}\n    check: {g['check']}")
    if not gates:
        click.echo("nothing waiting on you")


@partition_group.command("answer")
@click.argument("directory")
@click.argument("item")
@click.option("--pass", "passed", flag_value=True, default=None)
@click.option("--fail", "passed", flag_value=False)
@click.option("--reason", required=True)
@click.option("--choice", default=None)
def partition_answer(directory, item, passed, reason, choice):
    """Sign and send your verdict on ITEM's gate."""
    if passed is None:
        raise click.UsageError("say --pass or --fail")
    try:
        out = _open(directory).answer(item, passed=passed, reason=reason, choice=choice)
    except (PartitionError, HubRefusal) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{out.get('answer')}: {json.dumps(out.get('receipt', {}).get('receipt'))}")


@partition_group.command("held")
@click.argument("directory")
def partition_held(directory):
    """Answers the hub is holding because they arrived late."""
    held = _open(directory).held()
    for h in held:
        mark = f"you said {'PASS' if h['verdict']['passed'] else 'FAIL'}" if h["mine"] else "NOT SENT BY YOU"
        click.echo(f"{h['item']}  attempt {h['verifyAttempt']}  {mark}\n    {h['message']}")
    if not held:
        click.echo("nothing held")


@partition_group.command("confirm")
@click.argument("directory")
@click.argument("item")
def partition_confirm(directory, item):
    """Confirm a held answer by signing it again, fresh, from your own record."""
    try:
        out = _open(directory).confirm(item)
    except (PartitionError, HubRefusal) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{out.get('answer')}: {json.dumps(out.get('receipt', {}).get('receipt'))}")
