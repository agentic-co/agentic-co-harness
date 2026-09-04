#!/usr/bin/env python3
"""End to end: one plane, one ASOP, one run, three participants (P3.5).

    analyst      -> claude-code   a direct participant over signed HTTP (or MCP with --claude-code mcp)
    implementer  -> harness-bigmac  this runtime: pull, execute, report, attest
    validator    -> agy           a direct participant; interactive (OAuth), so a checkpoint

What it proves, in order, each printed as a checkpoint with PASS/FAIL:

  1. an ASOP authored on the plane is consumed by three different participants
  2. each executes only the steps bound to it; gates run where completion is recorded
  3. the run's parent closes in the write that lands the last step (§5.5) and
     outcomes reads it as done
  4. a good adjudication becomes a proposal, `propose` drafts v2 per step (§6.3)
  5. v2 activates, a v1 pin still resolves, a new run pins v2, outcomes has two rows,
     retire refuses new runs (§4, §2.1)
  6. separation of duties is refused at filing, not warned

Deterministic by default: the implementer's work is a prepared patch applied to a
tiny target repo, and the gate (`pytest`) is what proves it. `--live` lets the
runtime run its real backend instead. The analyst step is played over HTTP by
default; `--claude-code mcp` hands it to a headless `claude -p` through .mcp.json.

Usage:
    scripts/e2e/two_harnesses.py --hub-repo ~/Code/agentic-co-hub [--work-dir DIR]
                                 [--port 8791] [--gate human] [--live] [--claude-code http|mcp]
                                 [--auto-approve]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNTIME = HERE.parent.parent
sys.path.insert(0, str(RUNTIME))
from agentco_harness.hub_client import sign  # noqa: E402  (byte-identical to the plane's)

ACTORS = ["harness-bigmac", "claude-code", "agy", "mabidoli", "judge"]
HUMAN = "mabidoli"
#: A judged gate is answered by a DECLARED verifier holding the `verify`
#: capability, and never by the party that executed the step. Declaring the
#: capability is not the authority — `AGENTCO_VERIFIERS` is.
JUDGE = "judge"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# ----------------------------------------------------------------- a signed HTTP client per actor

class Plane:
    def __init__(self, url: str, keys: dict[str, str]):
        self.url, self.keys = url.rstrip("/"), keys

    def call(self, actor: str, method: str, path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload).encode() if payload is not None else b""
        ts = str(int(time.time()))
        req = urllib.request.Request(
            self.url + path, data=body if payload is not None else None, method=method,
            headers={"x-agentco-actor": actor, "x-agentco-timestamp": ts,
                     "x-agentco-signature": sign(self.keys[actor], method, path, ts, body),
                     "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return json.loads(e.read() or b"{}") | {"_http": e.code}

    def refused(self, resp: dict) -> str | None:
        return resp.get("code") if resp.get("state") == "refused" or "_http" in resp else None


# ----------------------------------------------------------------- fixtures

def write_target_repo(root: Path, with_patch: bool = True) -> None:
    """A real project with a real failing test. The implementer makes it pass.

    `with_patch` writes the prepared answer beside the problem — that patch IS
    the deterministic implementer's "work", so the run exercises the pipeline
    without a model. Under `--live` it must NOT be written: the first live run
    left it there and the model read it (session 53c271ea, 2026-09-04), so the
    step proved dispatch and proved nothing about solving anything. An answer
    key in the room is not a test of the room.
    """
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "slug.py").write_text("def slugify(s: str) -> str:\n    raise NotImplementedError\n")
    (root / "test_slug.py").write_text(textwrap.dedent('''
        from pkg.slug import slugify

        def test_spaces_become_dashes():
            assert slugify("Hello World") == "hello-world"

        def test_punctuation_is_dropped():
            assert slugify("Hi, there!") == "hi-there"
    '''))
    (root / "REQUIREMENT.md").write_text("# Feature: slugify\n\nTurn a title into a URL slug: lowercase, spaces to dashes, punctuation dropped.\n")
    if with_patch:
        (root / "IMPLEMENTATION.patch.py").write_text(textwrap.dedent('''
            import re
            def slugify(s: str) -> str:
                s = re.sub(r"[^\\w\\s-]", "", s.lower())
                return re.sub(r"[\\s_]+", "-", s).strip("-")
        '''))


def feature_dev_body(target: Path, gate5: str) -> dict:
    py = str(RUNTIME / ".venv" / "bin" / "python")     # the gate runs where the runtime runs; pytest is there
    # The plane requires the clock group on EVERY gate (ASOP.md §3.3: a parked
    # gate it cannot expire is a queue it cannot drain); a deterministic gate
    # that fails on timeout is the honest clock for one that cannot park.
    det = lambda check: {"kind": "deterministic", "check": check.replace("python3 -m pytest", f"{py} -m pytest"),
                         "cwd": str(target), "timeout_s": 120, "max_park_seconds": 3600, "on_timeout": "fail"}
    validate = ({"kind": "human", "check": "acceptance criteria traced to passing tests", "verifier": HUMAN,
                 "max_park_seconds": 86400, "on_timeout": "escalate", "escalate_to": HUMAN}
                if gate5 == "human" else
                {"kind": "judged", "check": "acceptance criteria traced to passing tests", "rubric": "e2e",
                 "max_park_seconds": 86400, "on_timeout": "escalate", "escalate_to": HUMAN})
    return {
        "title": "Develop a feature", "task_type": "feature",
        "purpose": "Take a specified feature from requirement to verified code.",
        "trigger": "A requirement exists with an owner.",
        "inputs": [{"name": "requirement", "description": "the requirement file"},
                   {"name": "repo", "description": "the repository the feature lands in"}],
        "roles": {"analyst": {"kind": "agent"}, "implementer": {"kind": "agent"}, "validator": {"kind": "agent"}},
        "constraints": [{"distinct": ["implementer", "validator"]}],
        "steps": [
            {"name": "validate-requirements", "role": "analyst",
             "purpose": "Read the requirement and write REQUIREMENTS.md listing each acceptance criterion.",
             "definition_of_done": "REQUIREMENTS.md exists and names every criterion in REQUIREMENT.md.",
             "gate": det("test -s REQUIREMENTS.md")},
            {"name": "write-tests", "role": "implementer", "purpose": "Tests exist for every criterion (they do: test_slug.py).",
             "gate": det("test -s test_slug.py"), "after": []},
            {"name": "implement", "role": "implementer", "purpose": "Make the tests pass.",
             "definition_of_done": "pytest passes in the target repo.", "gate": det("python3 -m pytest -q"),
             "after": [1, 2]},
            {"name": "run-tests", "role": "implementer", "purpose": "Prove it again from clean.", "gate": det("python3 -m pytest -q")},
            {"name": "validate", "role": "validator", "purpose": "Trace every criterion to a passing test.", "gate": validate},
        ],
    }


# ----------------------------------------------------------------- the runtime as implementer

def runtime_cli(node: Path, hub_repo: Path, args: list[str], env: dict) -> subprocess.CompletedProcess:
    exe = RUNTIME / ".venv" / "bin" / "harness"
    return subprocess.run([str(exe), "-c", str(node / "config.yaml"), *args], cwd=node, env=env,
                          capture_output=True, text=True)


def implementer_via_runtime(node: Path, hub_repo: Path, plane_url: str, secret: str, target: Path, live: bool) -> list[str]:
    env = {**os.environ, "AGENTCO_HUB_SECRET": secret}
    node.mkdir(parents=True, exist_ok=True)
    # `actor` is who this node is ON THE PLANE; `executor` is what runs the
    # work HERE. Without the second, every pulled bead fails the cycle with
    # "Unknown agent: harness-bigmac" — which is what --live found.
    (node / "config.yaml").write_text(textwrap.dedent(f'''
        tasks_path: tasks.jsonl
        instance: e2e-node
        hub:
          url: {plane_url}
          actor: harness-bigmac
          executor: claude
    '''))
    (node / "tasks.jsonl").touch()
    r = runtime_cli(node, hub_repo, ["hub", "status"], env); check("runtime: plane reachable and signature accepted", r.returncode == 0, r.stdout.strip() or r.stderr.strip())
    out: list[str] = []
    for round_ in range(4):                      # steps 2, 3, 4 become ready one after another
        r = runtime_cli(node, hub_repo, ["hub", "pull"], env)
        if "nothing ready" in r.stdout:
            break
        mirrored = [l.split()[0] for l in r.stdout.splitlines() if l.strip().startswith("ac-")]
        out += mirrored
        for bead in mirrored:
            if live:
                r2 = runtime_cli(node, hub_repo, ["cycle"], env)
            else:
                # the implementer's "work": apply the prepared patch, then let the gate prove it
                if (target / "IMPLEMENTATION.patch.py").exists():
                    shutil.copy(target / "IMPLEMENTATION.patch.py", target / "pkg" / "slug.py")
                r2 = runtime_cli(node, hub_repo, ["tasks", "complete", bead, "-r", json.dumps({"status": "complete", "output": "patch applied; gate proves it"})], env)
            print("    ", (r2.stdout or r2.stderr).strip().splitlines()[-1] if (r2.stdout or r2.stderr).strip() else "")
        r3 = runtime_cli(node, hub_repo, ["hub", "sync"], env)
        print("    ", r3.stdout.strip().splitlines()[-1] if r3.stdout.strip() else r3.stderr.strip())
    return out


# ----------------------------------------------------------------- a direct participant over HTTP

def participant_step(plane: Plane, actor: str, work, want_step: int) -> dict | None:
    """Pull as `actor` until the step we expect appears, do `work`, report with an attestation."""
    for _ in range(3):
        leased = plane.call(actor, "POST", "/work/pull", {"ttlSeconds": 600})
        if leased.get("state") != "leased":
            time.sleep(0.5); continue
        item, attempt = leased["item"], leased["attempt"]
        ref = (item.get("metadata") or {}).get("sop_ref") or {}
        if ref.get("step") != want_step:
            plane.call(actor, "POST", f"/work/{item['id']}/report", {"status": "failed", "attempt": attempt, "result": "not my step"})
            continue
        md = item.get("metadata") or {}
        gate = item.get("verify") or md.get("verify") or {}        # the plane's gate is top-level
        if not gate.get("check") and not gate.get("checks"):
            print(f"     (pulled item {item['id']} metadata keys: {sorted(md)}; gate keys: {sorted(gate)})")
        check_cmd = gate.get("check") or ((gate.get("checks") or [None])[0])
        exit_status = work({**gate, "check": check_cmd})
        payload = {"status": "done" if exit_status == 0 else "failed", "attempt": attempt,
                   "result": f"{actor} did step {want_step}"}
        if gate.get("kind") == "deterministic":
            # only a deterministic gate is attested by its executor (§5.3); a
            # human or judged gate parks and is answered by the party it names
            payload["attestation"] = {"check": check_cmd, "exit_status": exit_status,
                                      "environment": f"e2e-script actor={actor} cwd={gate.get('cwd')}",
                                      "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "submitted_by": actor}
        return plane.call(actor, "POST", f"/work/{item['id']}/report", payload)
    return None


def agy_via_mcp(plane_url: str, secret: str, target: Path, hub_repo: Path) -> bool:
    """agy v1.1.6: `agy mcp add` registers a stdio server; `--print` runs one prompt headless."""
    hub_py = str(hub_repo / ".venv" / "bin" / "python")
    subprocess.run(["agy", "mcp", "remove", "agentco"], capture_output=True)
    add = subprocess.run(["agy", "mcp", "add", "--env", "AGENTCO_ACTOR=agy", "--env", f"AGENTCO_REGISTRY_URL={plane_url}",
                          "--env", f"AGENTCO_SECRET={secret}", "agentco", "--", hub_py, "-m", "agentco", "serve-mcp"],
                         capture_output=True, text=True)
    if add.returncode != 0:
        print("     agy mcp add failed:", (add.stderr or add.stdout).strip()[:200]); return False
    prompt = ("You are the validator on a procedure. Use the agentco MCP tools: call work_pull to claim your step "
              "(it is the 'validate' step). Read REQUIREMENT.md, REQUIREMENTS.md and test_slug.py in the current directory and "
              "check every acceptance criterion maps to a test. Then call work_report with status done and the attempt you were "
              "given, and a one-line result naming the mapping. Do not attest — this step has a human gate and a person answers it.")
    try:
        r = subprocess.run(["agy", "--print", prompt, "--dangerously-skip-permissions"], cwd=target,
                           capture_output=True, text=True, timeout=600)
    finally:
        subprocess.run(["agy", "mcp", "remove", "agentco"], capture_output=True)
    print("     agy:", (r.stdout or r.stderr).strip()[-200:])
    return r.returncode == 0


def claude_code_via_mcp(plane_url: str, secret: str, target: Path, hub_repo: Path) -> bool:
    mcp = {"mcpServers": {"agentco": {"command": str(hub_repo / ".venv" / "bin" / "python"), "args": ["-m", "agentco", "serve-mcp"],
           "env": {"AGENTCO_ACTOR": "claude-code", "AGENTCO_REGISTRY_URL": plane_url, "AGENTCO_SECRET": secret}}}}
    (target / ".mcp.json").write_text(json.dumps(mcp, indent=2))
    prompt = ("You are the analyst on a procedure. Use the agentco MCP tools: call work_pull to claim your step, "
              "read REQUIREMENT.md, write REQUIREMENTS.md listing each acceptance criterion as a bullet, then call "
              "work_report with status done and the attempt you were given, and attest with check `test -s REQUIREMENTS.md`, "
              "exit_status 0. Do nothing else.")
    r = subprocess.run(["claude", "-p", prompt, "--mcp-config", str(target / ".mcp.json")], cwd=target, capture_output=True, text=True, timeout=600)
    return r.returncode == 0 and (target / "REQUIREMENTS.md").exists()


# ----------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub-repo", required=True, type=Path)
    ap.add_argument("--work-dir", type=Path, default=None)
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--gate", choices=["human", "judged"], default="human")
    ap.add_argument("--live", action="store_true", help="let the runtime run its real backend for implement")
    ap.add_argument("--claude-code", choices=["http", "mcp"], default="http")
    ap.add_argument("--agy", choices=["http", "mcp"], default="http", help="mcp: real headless agy as the validator")
    ap.add_argument("--auto-approve", action="store_true", help="answer the human gate as mabidoli without prompting")
    a = ap.parse_args()
    work = a.work_dir or Path(tempfile.mkdtemp(prefix="asop-e2e-"))
    work.mkdir(parents=True, exist_ok=True)
    plane_dir, target, node = work / "plane", work / "target", work / "node"
    plane_dir.mkdir(exist_ok=True); target.mkdir(exist_ok=True)
    hub_py = a.hub_repo / ".venv" / "bin" / "python"
    print(f"work dir: {work}")

    # 0. keys, target repo
    keys: dict[str, str] = {}
    for actor in ACTORS:
        out = subprocess.run([str(hub_py), "-m", "agentco", "keygen", actor], capture_output=True, text=True, cwd=plane_dir).stdout
        keys.update(json.loads(out[: out.index("}") + 1]))
    (plane_dir / "keys.json").write_text(json.dumps(keys, indent=2))
    write_target_repo(target, with_patch=not a.live)

    # 1. plane up
    env = {**os.environ, "AGENTCO_DB": str(plane_dir / "registry.sqlite3"), "AGENTCO_REGISTRY_KEYS": str(plane_dir / "keys.json"),
           "AGENTCO_HUMANS": HUMAN, "AGENTCO_ADJUDICATORS": "",
           # Declared verifiers. Undeclared, `verify` counts for nobody and a
           # judged gate can never be answered; declared, it counts only for
           # these actors, whatever anyone else claims in a payload.
           "AGENTCO_VERIFIERS": JUDGE}
    server = subprocess.Popen([str(hub_py), "-m", "agentco", "serve", "--port", str(a.port)], cwd=plane_dir, env=env,
                              stdout=open(plane_dir / "server.log", "w"), stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{a.port}"
    plane = Plane(url, keys)
    try:
        for _ in range(60):
            try:
                if plane.refused(plane.call(HUMAN, "GET", "/sops")) is None:
                    break
            except Exception:
                pass
            time.sleep(0.25)
        check("plane: up and accepting signed requests", plane.refused(plane.call(HUMAN, "GET", "/sops")) is None)

        # 2. author + activate v1
        created = plane.call(HUMAN, "POST", "/sops", feature_dev_body(target, a.gate) | {"sop_id": "feature-dev"})
        sop_id = (created.get("sop") or {}).get("asop_id") or (created.get("sop") or {}).get("sop_id") or "feature-dev"
        check("asop: v1 authored on the plane", created.get("state") == "accepted", json.dumps(created)[:160])
        act = plane.call(HUMAN, "POST", f"/sops/{sop_id}/activate", {"version": 1})
        check("asop: v1 active", act.get("state") == "accepted", json.dumps(act)[:160])

        # 6 (early). separation of duties refused at filing
        bad = plane.call(HUMAN, "POST", f"/sops/{sop_id}/run", {"inputs": {"requirement": "REQUIREMENT.md", "repo": str(target)},
                                                                 "bindings": {"analyst": "claude-code", "implementer": "agy", "validator": "agy"}})
        check("filing: validator == implementer is refused", plane.refused(bad) == "constraint_unsatisfiable", str(plane.refused(bad)))

        # 3. the run
        filed = plane.call(HUMAN, "POST", f"/sops/{sop_id}/run", {"inputs": {"requirement": "REQUIREMENT.md", "repo": str(target)},
                                                                   "bindings": {"analyst": "claude-code", "implementer": "harness-bigmac", "validator": "agy"}})
        run = filed.get("run") or {}
        run_id = run.get("id") or run.get("parentId") or run.get("runId") or run.get("parent")
        check("run: filed with three bindings", filed.get("state") == "accepted" and bool(run_id), json.dumps(filed)[:200])
        if not run_id:
            raise SystemExit("run was not filed; nothing downstream can be checked — see the FAIL above")
        steps = {s["step"]: s for s in run.get("steps", [])}
        print("     steps:", {k: (v["binding"], v["itemId"]) for k, v in sorted(steps.items())})

        # 4. analyst (claude-code)
        if a.claude_code == "mcp":
            ok = claude_code_via_mcp(url, keys["claude-code"], target, a.hub_repo)
            check("analyst: real Claude Code over MCP wrote REQUIREMENTS.md and reported", ok)
        else:
            def analyst_work(gate):
                (target / "REQUIREMENTS.md").write_text("- lowercase\n- spaces to dashes\n- punctuation dropped\n")
                return subprocess.run(gate["check"], shell=True, cwd=gate.get("cwd")).returncode
            r = participant_step(plane, "claude-code", analyst_work, 1)
            check("analyst: claude-code pulled step 1 only, reported done with attestation", bool(r) and r.get("state") in ("reported", "accepted", "done"), json.dumps(r)[:160])

        # 5. implementer (this runtime)
        mirrored = implementer_via_runtime(node, a.hub_repo, url, keys["harness-bigmac"], target, a.live)
        tree = plane.call(HUMAN, "GET", f"/runs/{run_id}").get("run") or {}
        by_step = {s["step"]: s for s in tree.get("steps", [])}
        done_steps = [k for k, v in by_step.items() if (v.get("status") or "").lower() == "done"]
        check("implementer: runtime executed steps 2-4 and the plane recorded them done", {2, 3, 4} <= set(done_steps), f"done={sorted(done_steps)} mirrored={mirrored}")
        check("execution: the analyst never touched steps 2-4", all(by_step[k].get("binding") == "harness-bigmac" for k in (2, 3, 4)))

        # 5b. validator (agy) — human gate: agy pulls, reports; mabidoli answers the gate
        if a.agy == "mcp":
            ok = agy_via_mcp(url, keys["agy"], target, a.hub_repo)
            check("validator: real agy over MCP pulled step 5 and reported", ok)
        else:
            r = participant_step(plane, "agy", lambda gate: 0, 5)
            check(f"validator: agy reported step 5 without attesting ({a.gate} gate)", bool(r) and plane.refused(r) is None, json.dumps(r)[:120])
        item5 = by_step[5]["itemId"]
        parked = plane.call(HUMAN, "GET", f"/runs/{run_id}").get("run") or {}
        st5 = {s["step"]: s for s in parked.get("steps", [])}[5].get("status")
        check(f"gate: the {a.gate} gate parked step 5 awaiting the verifier", (st5 or "").lower() == "awaiting_verify", str(st5))

        verdict = {"check": "acceptance criteria traced to passing tests", "exit_status": 0,
                   "environment": f"e2e {a.gate} verdict", "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if a.gate == "human":
            if not a.auto_approve:
                input("  >>> mabidoli: press Enter to answer the human gate (approve) ")
            ans = plane.call(HUMAN, "POST", f"/work/{item5}/attest", {"attestation": {**verdict, "submitted_by": HUMAN}})
            check("gate: the named verifier answered it", plane.refused(ans) is None, json.dumps(ans)[:160])
        else:
            # The rails, before the verdict. Both are the same rule seen twice:
            # authority to judge is declared by the operator, never claimed in a
            # payload, and never held by the party whose work is being judged.
            usurp = plane.call("claude-code", "POST", f"/work/{item5}/attest",
                               {"attestation": {**verdict, "submitted_by": "claude-code"}, "capabilities": ["verify"]})
            check("judged: an undeclared actor claiming `verify` is refused",
                  plane.refused(usurp) == "attestation_invalid", str(plane.refused(usurp)))
            selfjudge = plane.call("agy", "POST", f"/work/{item5}/attest",
                                   {"attestation": {**verdict, "submitted_by": "agy"}, "capabilities": ["verify"]})
            check("judged: the executor may not judge its own step",
                  plane.refused(selfjudge) == "attestation_invalid", str(plane.refused(selfjudge)))
            naked = plane.call(JUDGE, "POST", f"/work/{item5}/attest", {"attestation": {**verdict, "submitted_by": JUDGE}})
            check("judged: even a declared verifier must claim the capability",
                  plane.refused(naked) == "attestation_invalid", str(plane.refused(naked)))
            ans = plane.call(JUDGE, "POST", f"/work/{item5}/attest",
                             {"attestation": {**verdict, "submitted_by": JUDGE}, "capabilities": ["verify"]})
            check("gate: the declared verifier answered the judged gate", plane.refused(ans) is None, json.dumps(ans)[:200])

        # 3 (§5.5). auto-close + outcomes
        final = plane.call(HUMAN, "GET", f"/runs/{run_id}").get("run") or {}
        check("§5.5: the run's parent closed in the write that landed step 5", (final.get("status") or "").lower() == "done", str(final.get("status")))
        outcomes = plane.call(HUMAN, "GET", f"/sops/{sop_id}/outcomes")
        rows = outcomes.get("versions") or outcomes.get("outcomes") or outcomes.get("rows") or []
        v1 = next((r for r in rows if r.get("version") == 1), rows[0] if rows else {})
        check("outcomes: v1 shows 1 run, 1 done, 0 in flight", (v1.get("runs"), v1.get("done"), v1.get("inFlight")) == (1, 1, 0), json.dumps(v1)[:160])

        # 4. lessons: adjudicate step 3 good → propose → v2 draft carries it on step 3
        adj = plane.call(HUMAN, "POST", f"/work/{by_step[3]['itemId']}/adjudicate", {"verdict": "good", "evidence": "the procedure said pytest; the repo needs python3 -m pytest — fix the check"})
        check("lessons: a human adjudicated step 3 good with evidence", plane.refused(adj) is None, json.dumps(adj)[:160])
        selfadj = plane.call("harness-bigmac", "POST", f"/work/{by_step[3]['itemId']}/adjudicate", {"verdict": "good", "evidence": "me"})
        check("lessons: the executor may not adjudicate its own step", plane.refused(selfadj) in ("adjudication_self", "adjudication_exists", "adjudication_invalid"), str(plane.refused(selfadj)))
        prop = plane.call(HUMAN, "POST", f"/sops/{sop_id}/propose", {})
        draft = prop.get("sop") or prop.get("draft") or {}
        step3 = next((s for s in draft.get("steps", []) if s.get("step") == 3), {})
        check("lessons: propose drafted v2 with the proposal on step 3", draft.get("version") == 2 and bool(step3.get("proposals")), json.dumps(step3.get("proposals"))[:160])

        # 5. versioning
        act2 = plane.call(HUMAN, "POST", f"/sops/{sop_id}/activate", {"version": 2})
        check("versioning: v2 active, v1 superseded", act2.get("state") == "accepted")
        still = plane.call(HUMAN, "GET", f"/runs/{run_id}").get("run") or {}
        check("versioning: the v1 run still resolves at v1 after supersession (§2.1)", still.get("version") == 1, str(still.get("version")))
        run2 = plane.call(HUMAN, "POST", f"/sops/{sop_id}/run", {"inputs": {"requirement": "REQUIREMENT.md", "repo": str(target)},
                                                                  "bindings": {"analyst": "claude-code", "implementer": "harness-bigmac", "validator": "agy"}})
        check("versioning: a new run pins v2", run2.get("state") == "accepted" and (run2.get("run") or {}).get("version") == 2, str((run2.get("run") or {}).get("version")))
        out2 = plane.call(HUMAN, "GET", f"/sops/{sop_id}/outcomes")
        rows2 = out2.get("versions") or out2.get("outcomes") or out2.get("rows") or []
        check("versioning: outcomes now has a row per version", len(rows2) >= 2, f"{len(rows2)} rows")
        ret = plane.call(HUMAN, "POST", f"/sops/{sop_id}/retire", {})
        blocked = plane.call(HUMAN, "POST", f"/sops/{sop_id}/run", {"inputs": {"requirement": "x", "repo": "y"}, "bindings": {"analyst": "claude-code", "implementer": "harness-bigmac", "validator": "agy"}})
        check("versioning: retire refuses new runs", ret.get("state") == "accepted" and plane.refused(blocked) == "sop_refused", str(plane.refused(blocked)))
    finally:
        server.terminate()
        try:
            server.wait(5)
        except subprocess.TimeoutExpired:
            server.kill()

    print("\n" + "=" * 72)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"{passed}/{len(RESULTS)} checkpoints passed   work dir: {work}   plane log: {plane_dir / 'server.log'}")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL  {name}: {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
