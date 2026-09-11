#!/usr/bin/env python3
"""SWE-bench Verified as ASOP runs: prepare instances, file them, score them.

Stage 0 of the eval plan. What this proves is not a resolve rate — 20 instances
cannot support one — but that the whole path works on real work: a dataset
instance becomes a filed run with a workdir and a deterministic gate, an agent
backend executes it, the gate decides, and cost lands in the ledger.

**The leakage boundary is the one thing here that must not be got wrong.**
A SWE-bench instance carries three things the agent must never see:

  * `patch` — the gold solution;
  * `test_patch` — the tests that decide the outcome;
  * `FAIL_TO_PASS` / `PASS_TO_PASS` — their names.

`FAIL_TO_PASS` tests do not exist at `base_commit`; they arrive in
`test_patch`. So the honest construction falls out of the data: the agent gets
a checkout at `base_commit` and the issue text, and the gate applies
`test_patch` inside a throwaway container the agent never touches. Anything
that puts the tests in the workdir is measuring recall of the answer.

**Why the official per-instance images.** Each SWE-bench instance needs its own
interpreter and pinned dependencies at a specific commit, and reproducing that
is the part of SWE-bench that eats weeks. `swebench/sweb.eval.x86_64.<id>`
already has it, at `/testbed`. We copy the repo OUT of the image to a host
workdir for the agent to edit, and copy it back IN for the gate to judge. No
bind mounts: the images run under x86_64 emulation on arm64, and mount
semantics across that boundary are one more thing that can silently differ.

Usage:
    swebench.py prepare --count 20 --out <dir>     # sample, pull, materialise
    swebench.py file --manifest <dir>/manifest.json --node <path>
    swebench.py gate --manifest ... --instance <id>   # what the gate runs
    swebench.py score --manifest ... --node <path>
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

DATASET = "princeton-nlp/SWE-bench_Verified"
ROWS_URL = "https://datasets-server.huggingface.co/rows"
IMAGE = "swebench/sweb.eval.x86_64.{slug}:latest"
PLATFORM = "linux/amd64"

SPECS = json.loads((Path(__file__).parent / "swebench_test_cmds.json").read_text())["specs"]


def test_cmd_for(repo: str, version: str) -> str:
    """The runner this repo actually uses, at this version.

    Vendored from swebench 2.1.8 rather than imported: swebench>=5 expects a
    dataset schema carrying `eval_script` per row, which the published
    SWE-bench_Verified rows do not have, and importing the package pulls in
    modal and fails. The map is small, stable and cited in the JSON's own
    `_source`. A missing entry raises rather than defaulting to pytest —
    guessing the runner is how this went wrong the first time.
    """
    try:
        return SPECS[repo][version]["test_cmd"]
    except KeyError:
        raise SystemExit(
            f"no test command known for {repo} @ {version}. Refusing to guess: "
            f"a wrong runner reports every instance unresolved, silently."
        )


# The dataset's own id convention. `__` is not legal in a docker tag, and the
# published images use this substitution rather than a hash, so it has to be
# reproduced exactly or every pull 404s.
def image_for(instance_id: str) -> str:
    return IMAGE.format(slug=instance_id.replace("__", "_1776_"))


def fetch_rows(limit: int = 500) -> list[dict]:
    """Every instance, in dataset order. 100 per request is the server's cap."""
    rows: list[dict] = []
    while len(rows) < limit:
        q = urllib.parse.urlencode(
            {"dataset": DATASET, "config": "default", "split": "test",
             "offset": len(rows), "length": min(100, limit - len(rows))}
        )
        with urllib.request.urlopen(f"{ROWS_URL}?{q}", timeout=120) as r:
            page = json.load(r)
        got = [x["row"] for x in page.get("rows", [])]
        if not got:
            break
        rows.extend(got)
    return rows


def sample(rows: list[dict], count: int, seed: int) -> list[dict]:
    """Stratified by the Verified difficulty label, proportional to the split.

    Not a random draw. Verified is difficulty-annotated, and a uniform sample of
    20 from 500 can land mostly in one band — which would make a pilot's numbers
    look like a capability claim when they are a sampling artefact. Stratifying
    costs nothing and removes the temptation.
    """
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        buckets.setdefault(str(r.get("difficulty", "unknown")), []).append(r)
    rng = random.Random(seed)
    picked: list[dict] = []
    for name in sorted(buckets):
        pool = buckets[name]
        take = max(1, round(count * len(pool) / len(rows)))
        rng.shuffle(pool)
        picked.extend(pool[:take])
    rng.shuffle(picked)
    return picked[:count]


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def prepare(count: int, out: Path, seed: int) -> int:
    out.mkdir(parents=True, exist_ok=True)
    print(f"[swebench] fetching {DATASET} …")
    rows = fetch_rows()
    picked = sample(rows, count, seed)
    print(f"[swebench] sampled {len(picked)} of {len(rows)} (seed {seed})")

    manifest = []
    for i, row in enumerate(picked, 1):
        iid = row["instance_id"]
        image = image_for(iid)
        workdir = out / "work" / iid
        print(f"[{i}/{len(picked)}] {iid} ({row.get('difficulty')}) — pulling …")
        pull = run(["docker", "pull", "--platform", PLATFORM, image])
        if pull.returncode != 0:
            print(f"    SKIP: image unavailable — {pull.stderr.strip()[:120]}")
            continue

        # Copy the repo OUT for the agent. The container is created, never
        # started: we want the filesystem, not a running environment.
        workdir.parent.mkdir(parents=True, exist_ok=True)
        if workdir.exists():
            run(["rm", "-rf", str(workdir)])
        cid = run(["docker", "create", "--platform", PLATFORM, image]).stdout.strip()
        if not cid:
            print("    SKIP: could not create container")
            continue
        cp = run(["docker", "cp", f"{cid}:/testbed", str(workdir)])
        run(["docker", "rm", "-f", cid])
        if cp.returncode != 0:
            print(f"    SKIP: docker cp failed — {cp.stderr.strip()[:120]}")
            continue

        # The answer and the tests stay HERE, next to the manifest, and never
        # go near the workdir the agent is pointed at.
        manifest.append({
            "instance_id": iid,
            "repo": row["repo"],
            "base_commit": row["base_commit"],
            "difficulty": row.get("difficulty"),
            "version": row["version"],
            "image": image,
            "workdir": str(workdir.resolve()),
            "problem_statement": row["problem_statement"],
            "test_patch": row["test_patch"],
            "fail_to_pass": json.loads(row["FAIL_TO_PASS"]),
            "pass_to_pass": json.loads(row["PASS_TO_PASS"]),
        })
        print(f"    ready: {workdir}")

    path = out / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2))
    print(f"[swebench] {len(manifest)} instance(s) ready — {path}")
    return 0 if manifest else 1


def gate(manifest_path: Path, instance_id: str, run_id: str) -> int:
    """The deterministic check. Exit 0 iff the official grader says resolved.

    This delegates to `swebench.harness.run_evaluation` rather than deciding
    for itself, and the first version of this file did decide for itself. That
    version was wrong in a way worth recording, because it is the exact failure
    this project keeps writing tests about:

      * it ran `pytest` for every repo. Django runs `./tests/runtests.py` and
        sympy runs `bin/test`, so those instances failed the gate no matter
        what the agent did;
      * it printed stdout, and Django's runner reports on stderr, so the
        failure was SILENT;
      * it passed FAIL_TO_PASS entries as test IDs. For Django they are
        unittest display names — one of them is literally
        `#24155 - Tests ordering of imports.`, a docstring — so the runner
        ignored them and ran all 16,223 tests.

    Each of those reports "unresolved" for a correct patch. A grader that can
    only say no is not a grader, and none of it is visible from a green run.

    The official harness scopes the run from `test_patch` and parses the log
    with a per-repo parser (23 of them). Validated both directions before being
    trusted here: the gold patch grades RESOLVED, an empty patch does not.

    The agent's work reaches the grader as a diff of its workdir, which is also
    what keeps the boundary honest — the grader is handed a patch, never the
    workdir, so nothing the agent wrote to disk can influence how it is judged.
    """
    entries = {e["instance_id"]: e for e in json.loads(manifest_path.read_text())}
    e = entries.get(instance_id)
    if e is None:
        print(f"gate: {instance_id} not in manifest", file=sys.stderr)
        return 2

    diff = run(["git", "-C", e["workdir"], "diff"])
    if diff.returncode != 0:
        print(f"gate: could not diff the workdir — {diff.stderr.strip()[:160]}", file=sys.stderr)
        return 2

    out = Path(e["workdir"]).parent.parent / "predictions" / f"{instance_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([{
        "instance_id": instance_id,
        "model_name_or_path": run_id,
        "model_patch": diff.stdout,
    }]))

    res = run(["uv", "run", "--with", "swebench==3.0.15", "--no-project", "python",
               "-m", "swebench.harness.run_evaluation",
               "--dataset_name", DATASET,
               "--predictions_path", str(out),
               "--instance_ids", instance_id,
               "--run_id", run_id,
               "--max_workers", "1",
               "--cache_level", "instance"], cwd=str(out.parent))
    report = out.parent / f"{run_id}.{run_id}.json"
    if not report.exists():
        matches = sorted(out.parent.glob(f"*.{run_id}.json"))
        report = matches[0] if matches else None
    if report is None:
        print(f"gate: no report produced — {(res.stdout or res.stderr)[-400:]}", file=sys.stderr)
        return 2

    data = json.loads(report.read_text())
    resolved = instance_id in (data.get("resolved_ids") or [])
    print(f"{instance_id}: {'RESOLVED' if resolved else 'unresolved'}"
          f"  (empty_patch={instance_id in (data.get('empty_patch_ids') or [])})")
    return 0 if resolved else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare")
    p.add_argument("--count", type=int, default=20)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--seed", type=int, default=20260911)

    g = sub.add_parser("gate")
    g.add_argument("--manifest", type=Path, required=True)
    g.add_argument("--instance", required=True)
    g.add_argument("--run-id", default="asop-eval")

    a = ap.parse_args()
    if a.cmd == "prepare":
        return prepare(a.count, a.out, a.seed)
    if a.cmd == "gate":
        return gate(a.manifest, a.instance, a.run_id)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
