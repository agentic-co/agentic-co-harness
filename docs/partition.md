# Your partition: answering a hub's human gates as yourself

A company hub can name you as the person who signs off a gate. You answer from a **partition**
on your own machine. The partition holds a private key for that hub and signs your verdict as an
`asop.answer/1` envelope. A transport credential carries the envelope to the hub. The hub holds
only your public key, so it can check that an answer is yours, but it can't write one for you. The
contract is the hub's [`docs/envelope.md`](https://github.com/agentic-co/agentic-co-hub/blob/main/docs/envelope.md),
from ADR 0008.

You need one partition per company hub, and you never reuse a key for another hub.

```bash
pip install 'agentco-harness[partition]'
export AGENTCO_PARTITION_SECRET=...   # the transport secret the hub's operator gave you; never stored

agentic-co partition init ~/.agentco/partitions/acme \
  --hub https://hub.acme.example --label acme --person dana --transport relay-dana
```

`init` creates the key and pins the hub's generated id. It prints your **public** key, which you
hand to the hub's operator, and a **fingerprint**. Read the fingerprint to the operator on a
different channel, such as a call or in person. They type it when they enrol the key
(`agentco human-key enrol`). A fingerprint that doesn't match is refused, and that check is what
catches a swapped key.

Then:

```bash
agentic-co partition gates   ~/.agentco/partitions/acme            # what is waiting on you
agentic-co partition answer  ~/.agentco/partitions/acme w-123 --pass --reason "rollback exercised"
agentic-co partition held    ~/.agentco/partitions/acme            # answers that arrived late
agentic-co partition confirm ~/.agentco/partitions/acme w-123      # re-sign one, from your own record
```

The partition directory holds three files:

- **`partition.json`:** the hub and who you are on it.
- **`key.pem`:** your private key, mode 0600. The directory itself is 0700.
- **`envelopes.jsonl`:** every envelope you signed, and what the hub said back.

The ledger is your evidence, and it is what `confirm` reads. Suppose the hub holds an answer that
arrived late (24 to 36 hours after signing, by default). `held` matches it against your ledger by
digest. If it doesn't match, it reads **"the hub reports an answer you did not send"** and there's
nothing to confirm. If it matches, `confirm` signs a **new** envelope with the verdict you
recorded, never one the hub reports.

`scripts/e2e/partition_loop.sh` runs the whole loop against a live hub from a clone of
agentic-co-hub. It files a human-gated item, sets up the partition, enrols the key and answers the
gate, and the answer lands as you, through the relay.
