#!/usr/bin/env bash
# The first end-to-end loop of hub ADR 0008: a live hub, a human-gated item
# worked to its gate, `agentic-co partition init`, the operator enrolling the
# key by the fingerprint, then the gate answered from the partition and landing
# as the person. Needs a clone of agentic-co-hub: AGENTCO_HUB_CHECKOUT=... bash
# scripts/e2e/partition_loop.sh. Synthetic actors; everything in a temp dir.
set -u
HUB=${AGENTCO_HUB_CHECKOUT:?set AGENTCO_HUB_CHECKOUT to a clone of agentic-co-hub}; HAR=$(cd "$(dirname "$0")/../.." && pwd)
D=$(mktemp -d); export AGENTCO_REGISTRY_KEYS=$D/keys.json AGENTCO_REGISTRY_DB=$D/r.sqlite3 \
  AGENTCO_WORK_STORE=$D/work.jsonl AGENTCO_SOP_STORE=$D/sops.jsonl ASOP_HUMANS=dana \
  AGENTCO_HUB_ID=acme-hub AGENTCO_REGISTRY_OPERATOR=op
cat > $D/keys.json <<K
{"op": "op-secret", "dana": "dana-secret", "hub-worker": "hw-secret",
 "relay-dana": {"secret": "rd-secret", "owner": "dana", "class": "transport"}}
K
chmod 600 $D/keys.json
cd $HUB && uv run --extra server --extra mcp --extra envelope agentco serve --port 18765 > $D/serve.log 2>&1 &
trap 'lsof -ti tcp:18765 | xargs kill 2>/dev/null' EXIT
for i in $(seq 1 40); do curl -s -o /dev/null 127.0.0.1:18765/docs && break; sleep 0.25; done
C="uv run --directory $HUB --extra server python $HAR/scripts/e2e/hub_call.py"
echo "== 1. file a human-gated item, work it to its gate"
ITEM=$($C hub-worker POST /work '{"title":"ship the rollback","assignedAgent":"hub-worker","verify":{"kind":"human","check":"a person confirms the rollback was exercised","verifier":"dana","max_park_seconds":3600,"on_timeout":"escalate","escalate_to":"dana"}}' | python3 -c "import json,sys; print(json.load(sys.stdin)['item']['id'])")
ATT=$($C hub-worker POST /work/pull '{}' | python3 -c "import json,sys; print(json.load(sys.stdin)['attempt'])")
$C hub-worker POST /work/$ITEM/report "{\"status\":\"done\",\"attempt\":$ATT}" | python3 -c "import json,sys; print('status:', json.load(sys.stdin)['item']['status'])"
(cd $HUB && uv run agentco verifiers --route --json >/dev/null) && echo "vehicle routed to dana"
echo "== 2. partition init (harness)"
export AGENTCO_PARTITION_SECRET=rd-secret
(cd $HAR && uv run agentic-co partition init $D/partition --hub http://127.0.0.1:18765 --label acme --person dana --transport relay-dana) > $D/init.out
head -1 $D/init.out
sed -n '/BEGIN PUBLIC KEY/,/END PUBLIC KEY/p' $D/init.out > $D/dana.pub.pem
FP=$(tail -1 $D/init.out | tr -d ' ')
echo "== 3. operator enrols the key, typing the fingerprint read aloud"
(cd $HUB && uv run --extra envelope agentco human-key enrol dana $D/dana.pub.pem --fingerprint "$FP" --by op) | head -1
echo "== 4. unsigned attest in dana's name is now refused"
$C dana POST /work/$ITEM/attest '{"attestation":{"check":"x","exit_status":0,"environment":"laptop","at":"2026-10-30T00:00:00+00:00","verdict":{"passed":true,"reason":"seen"}}}' | python3 -c "import json,sys; print('unsigned:', json.load(sys.stdin)['code'])"
echo "== 5. partition sees the gate and answers"
(cd $HAR && uv run agentic-co partition gates $D/partition)
(cd $HAR && uv run agentic-co partition answer $D/partition $ITEM --pass --reason "rollback exercised on staging")
echo "== 6. the hub's record"
$C dana GET /work/$ITEM | python3 -c "
import json,sys; i=json.load(sys.stdin)['item']; a=i['attestation']
print('status:', i['status'], '| submitted_by:', a['submitted_by'], '| via:', a['envelope']['via'], '| key:', a['envelope']['key_id'])"
echo "== 7. the relay cannot reach unsigned attest, and the answered gate is gone"
$C relay-dana POST /work/$ITEM/attest '{}' | python3 -c "import json,sys; print('relay attest:', json.load(sys.stdin)['code'])"
(cd $HAR && uv run agentic-co partition gates $D/partition)
grep -iE -A3 "error|traceback" $D/serve.log | head -30
