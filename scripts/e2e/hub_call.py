"""Signed HTTP to the hub as a named actor: call.py ACTOR METHOD PATH [JSON]."""
import json, sys, time, urllib.request, urllib.error, os
from agentco import auth
keys = json.load(open(os.environ["AGENTCO_REGISTRY_KEYS"]))
actor, method, path = sys.argv[1:4]
body = sys.argv[4].encode() if len(sys.argv) > 4 else b""
secret = keys[actor] if isinstance(keys[actor], str) else keys[actor]["secret"]
p, _, q = path.partition("?")
ts = str(int(time.time()))
req = urllib.request.Request("http://127.0.0.1:18765" + path, data=body or None, method=method, headers={
    "X-AgentCo-Actor": actor, "X-AgentCo-Timestamp": ts,
    "X-AgentCo-Signature": auth.sign(secret, method, p, ts, body, query=q or None),
    "Content-Type": "application/json"})
try:
    print(urllib.request.urlopen(req).read().decode())
except urllib.error.HTTPError as e:
    print(e.read().decode())
