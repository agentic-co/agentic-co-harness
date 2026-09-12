#!/usr/bin/env python3
"""τ-bench airline as an ASOP evolution experiment: split, gate, self-check.

The POC this serves: take Sierra's airline `policy.md` as ASOP **v1**, run it,
adjudicate the DEV failures, let `propose` draft **v2**, and re-run the HELD-OUT
tasks with the same model. The question is not "can an agent book a flight" —
it is whether a procedure that has been through the loop does better than the
one that has not.

**Why this domain.** `policy.md` is 1,313 words already organised as
procedures — Book flight, Modify flight, Cancel flight, Refunds and
Compensation — each with preconditions and rules. It is an ASOP wearing
different clothes, which means we adopt a procedure somebody else wrote rather
than authoring the thing we are also grading.

**Why ENV-only scoring.** τ-bench can score on environment state, on what the
agent communicated, and on which tools it called. Only the first is
deterministic: `evaluator_env` compares a hash of the final database against a
gold database built by REPLAYING the task's recorded actions. Communication is
judged. An experiment about whether procedures improve verification must not
rest its own verdict on a judge, so the gate here reads `EvaluationType.ENV`
and nothing else.

**The split is the methodology.** Adjudicate on DEV, measure on TEST, and never
let TEST inform a revision. Re-running the tasks you learned from measures
leakage and calls it improvement. The split is seeded and written to the
manifest so a later run cannot quietly re-draw it.

Usage:
    taubench.py prepare --tau2 <path> --out <dir> [--dev 12 --test 12]
    taubench.py selfcheck --tau2 <path> --out <dir>   # no API calls, no cost
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

DOMAIN = "airline"


def _load_tau2(tau2: Path):
    """Put tau2 on the path and hand back the pieces we need.

    Imported lazily and by path because tau2-bench is a checkout rather than a
    dependency of this runtime — the experiment borrows it, and the runtime
    must not grow a dependency on a benchmark.
    """
    src = tau2 / "src"
    if not src.is_dir():
        raise SystemExit(f"no tau2 checkout at {tau2} (expected {src})")
    sys.path.insert(0, str(src))
    # The domain's environment directly, NOT `tau2.registry`. The registry
    # imports the batch runner, which imports the simulation model, which
    # imports the voice stack, which needs `websockets` — a dependency chain
    # ending in a realtime audio provider, for a check that never makes a
    # network call. Importing the narrow thing keeps the self-check runnable in
    # an environment that could not possibly run a voice benchmark.
    from tau2.data_model.tasks import Task  # noqa: E402
    from tau2.domains.airline.environment import get_environment  # noqa: E402

    return Task, get_environment


def _tasks(tau2: Path) -> list[dict]:
    raw = json.loads((tau2 / "data/tau2/domains" / DOMAIN / "tasks.json").read_text())
    return raw if isinstance(raw, list) else raw.get("tasks", [])


def discriminating(tau2: Path) -> tuple[list[str], list[str]]:
    """Split the domain into tasks this gate can grade, and tasks it cannot.

    ENV scoring compares a hash of the final database. Roughly half of the
    airline tasks never change it — the user asks a question and the agent
    answers — so an agent that did nothing at all produces the same hash as one
    that did the task correctly. Measured: 26 of 50 gradeable, 24 read-only.

    Including those would not be a small inaccuracy. They are not noise, they
    are free marks: every one scores PASS for an agent that sat still, and a
    procedure change could not move them in either direction. A pass rate built
    on them would look like a result and measure nothing.

    So the split is drawn from the gradeable pool only, and the count of what
    was excluded goes in the manifest rather than being quietly dropped. Grading
    the other half honestly needs the COMMUNICATE criteria, which are judged by
    a model — a different experiment, and not one to fold into this one.
    """
    Task, get_environment = _load_tau2(tau2)
    gradeable: list[str] = []
    read_only: list[str] = []
    for raw in _tasks(tau2):
        task = Task.model_validate(raw)
        actions = (task.evaluation_criteria.actions or []) if task.evaluation_criteria else []
        if not actions:
            read_only.append(task.id)
            continue
        init = task.initial_state
        kw = dict(
            initialization_data=(init.initialization_data if init else None),
            initialization_actions=(init.initialization_actions if init else None),
            message_history=[],
        )
        gold = get_environment()
        gold.set_state(**kw)
        for a in actions:
            gold.make_tool_call(a.name, **(a.arguments or {}))
        untouched = get_environment()
        untouched.set_state(**kw)
        (gradeable if untouched.get_db_hash() != gold.get_db_hash()
         else read_only).append(task.id)
    return gradeable, read_only


def prepare(tau2: Path, out: Path, n_dev: int, n_test: int, seed: int) -> int:
    """Draw the DEV/TEST split once and write it down."""
    out.mkdir(parents=True, exist_ok=True)
    ids, read_only = discriminating(tau2)
    if len(ids) < n_dev + n_test:
        raise SystemExit(
            f"{DOMAIN} has {len(ids)} gradeable tasks (of {len(ids) + len(read_only)}); "
            f"need {n_dev + n_test}"
        )

    rng = random.Random(seed)
    shuffled = list(ids)
    rng.shuffle(shuffled)
    dev, test = shuffled[:n_dev], shuffled[n_dev:n_dev + n_test]

    policy = (tau2 / "data/tau2/domains" / DOMAIN / "policy.md").read_text()
    manifest = {
        "domain": DOMAIN,
        "seed": seed,
        "gradeable_tasks": len(ids),
        "read_only_excluded": len(read_only),
        # DEV is where failures get adjudicated. TEST is never adjudicated and
        # never seen while a revision is being drafted — written here so the
        # boundary is a fact on disk rather than an intention.
        "dev": dev,
        "test": test,
        "scoring": "EvaluationType.ENV — database-state hash only, no judge",
        "v1_policy_sha": __import__("hashlib").sha256(policy.encode()).hexdigest()[:16],
        "v1_policy_words": len(policy.split()),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (out / "policy.v1.md").write_text(policy)
    print(f"[taubench] {len(ids)} gradeable of {len(ids) + len(read_only)} "
          f"({len(read_only)} read-only, excluded); DEV={len(dev)} TEST={len(test)} "
          f"(seed {seed})")
    print(f"[taubench] v1 policy: {manifest['v1_policy_words']} words, "
          f"sha {manifest['v1_policy_sha']}")
    print(f"[taubench] manifest -> {out / 'manifest.json'}")
    return 0


def selfcheck(tau2: Path, out: Path) -> int:
    """Prove the gate can say BOTH yes and no, without spending anything.

    A gate that only ever refuses scores every run unresolved and looks like a
    capability finding; a gate that only ever passes is worse. Neither is
    visible from a run that exits cleanly, which is why this exists before any
    model is invoked.

    Both directions come free because the gold environment is built by
    REPLAYING the task's recorded actions — no agent, no user simulator, no
    API call:

      * replay the gold actions  -> the hashes must match   (the gate can pass)
      * replay nothing           -> they must differ        (the gate can fail)

    A task whose gold actions leave the database untouched proves nothing in
    the second direction, so it is reported as INCONCLUSIVE rather than counted
    as a pass. Silently counting those is how a gate that cannot fail gets a
    green self-check.
    """
    Task, get_environment = _load_tau2(tau2)
    manifest = json.loads((out / "manifest.json").read_text())
    wanted = set(manifest["dev"] + manifest["test"])
    raw = [t for t in _tasks(tau2) if t["id"] in wanted]

    env_ctor = get_environment
    passed = failed = inconclusive = 0

    for t in raw:
        task = Task.model_validate(t)
        actions = (task.evaluation_criteria.actions or []) if task.evaluation_criteria else []
        if not actions:
            inconclusive += 1
            continue

        init = task.initial_state
        kw = dict(
            initialization_data=(init.initialization_data if init else None),
            initialization_actions=(init.initialization_actions if init else None),
            message_history=[],
        )

        gold = env_ctor()
        gold.set_state(**kw)
        for a in actions:
            gold.make_tool_call(a.name, **(a.arguments or {}))

        untouched = env_ctor()
        untouched.set_state(**kw)

        replayed = env_ctor()
        replayed.set_state(**kw)
        for a in actions:
            replayed.make_tool_call(a.name, **(a.arguments or {}))

        can_pass = replayed.get_db_hash() == gold.get_db_hash()
        can_fail = untouched.get_db_hash() != gold.get_db_hash()

        if can_pass and can_fail:
            passed += 1
        elif can_pass and not can_fail:
            # The gold actions are read-only for this task: doing nothing looks
            # identical to doing it right. Not a gate failure — a task this
            # gate cannot grade, and it must not be counted either way.
            inconclusive += 1
        else:
            failed += 1
            print(f"  GATE BROKEN on {task.id}: replay!=gold "
                  f"(can_pass={can_pass} can_fail={can_fail})")

    total = passed + failed + inconclusive
    print(f"[taubench] gate self-check over {total} task(s):")
    print(f"    {passed:3d} discriminate correctly (replay passes, no-op fails)")
    print(f"    {inconclusive:3d} inconclusive — gold actions do not change the database")
    print(f"    {failed:3d} BROKEN")
    if failed:
        print("[taubench] refusing: a gate that cannot reproduce its own gold "
              "state cannot grade an agent.", file=sys.stderr)
        return 1
    if not passed:
        print("[taubench] refusing: no task proved the gate can fail.", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("prepare", "selfcheck"):
        s = sub.add_parser(name)
        s.add_argument("--tau2", type=Path, required=True)
        s.add_argument("--out", type=Path, required=True)
        if name == "prepare":
            s.add_argument("--dev", type=int, default=12)
            s.add_argument("--test", type=int, default=12)
            s.add_argument("--seed", type=int, default=20260912)

    a = ap.parse_args()
    if a.cmd == "prepare":
        return prepare(a.tau2, a.out, a.dev, a.test, a.seed)
    return selfcheck(a.tau2, a.out)


if __name__ == "__main__":
    raise SystemExit(main())
