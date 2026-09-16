#!/usr/bin/env python3
"""Arm B — the control run_arm_c.py has never had a number to be compared against.

    python3 scripts/eval/run_arm_b.py --tau2 <checkout> --data evals/tau2-airline-asop \\
        --out <dir> --trials 2 --agent-llm anthropic/glm-4.7

Arm B is tau2's OWN agent reading tau2's OWN policy: no ASOP, no stepwise
presentation, no gate. It is the number that decides whether "ASOP" names
anything. Arm C's 0.423 is uninterpretable without it — a gated procedure that
does not beat the stock prompt has been measuring its own overhead.

WHY NOT run_arms.sh
-------------------
`run_arms.sh` already defines this arm and drives it through the `tau2` CLI,
which is correct for the local models it was written for. It cannot drive an
Anthropic-routed model: tau2 opens with a system-only message array, which
every OpenAI-compatible endpoint accepts and Anthropic's rejects outright, and
the fix (`litellm.modify_params`) is a library setting with no CLI surface.
This script is `run_arm_c.py` with the agent left alone, so that arm B and
arm C differ in the thing under test and in nothing else — same task list,
same user simulator, same evaluation type, same modify_params compromise.

The policy is NOT swapped here. Arm C copies an ASOP over the canonical policy
path; arm B wants exactly what is already there. It is still backed up and
restored, because a crash mid-run that leaves somebody else's ASOP in place is
the failure this whole family of scripts is shaped around.
"""

from __future__ import annotations

import argparse
import collections
import json
import shutil
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tau2", type=Path, required=True)
    ap.add_argument(
        "--defer-consent-stop",
        action="store_true",
        help="give the agent one more turn when the user grants consent and stops in the "
        "same turn. CHANGES CONVERSATION DYNAMICS — a run with this flag is not comparable "
        "with one without it, so re-run the baseline alongside.",
    )
    ap.add_argument(
        "--domain",
        default="airline",
        help="tau2 domain. `retail` is the second dataset (114 tasks, its own policy.md, "
        "same EvaluationType.ENV scoring) — the point of it is that a conclusion drawn on "
        "one domain's policy is a conclusion about that policy.",
    )
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument(
        "--policy",
        default=None,
        help="document (relative to --data) to serve AS the policy, e.g. asops/asop.claude.v2.md. "
        "Omit for the stock tau2 policy. With this set the agent reads the WHOLE document — "
        "rules, gates and routing all visible at once — and no gate is ever evaluated.",
    )
    ap.add_argument("--tasks", nargs="*", default=None, help="task ids; default = split TEST")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--agent-llm", default="openai/openai/gpt-oss-20b")
    ap.add_argument("--user-llm", default="openai/google/gemma-4-31b")
    ap.add_argument("--max-steps", type=int, default=60)
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    # Same stdin-resume trap as arm C: tau2 offers to resume when results.json
    # is already there and asks on stdin, which blocks forever under nohup.
    (args.out / "results.json").unlink(missing_ok=True)

    sys.path.insert(0, str(args.tau2 / "src"))

    import litellm

    litellm.modify_params = True

    # Two measured scoring faults; see scripts/eval/harness_fixes.py for both and for
    # why only one of them is fixed rather than detected.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import harness_fixes

    print(f"[arm b] FIX: {harness_fixes.canonicalise_payment_history(args.domain)}")
    if args.defer_consent_stop:
        print(f"[arm b] FIX: {harness_fixes.defer_consent_stop()}")

    from tau2.data_model.tasks import Task
    from tau2.run import run_tasks
    from tau2.evaluator.evaluator import EvaluationType

    policy = args.tau2 / f"data/tau2/domains/{args.domain}/policy.md"
    backup = args.out / "policy.original.md"
    if not backup.exists():
        shutil.copy(policy, backup)

    # Arm B is only honest if the policy in place is the stock one. Arm C's
    # restore has failed before; check rather than assume. The check is against
    # what is on disk BEFORE any swap, so it still catches a previous arm's
    # leftovers even when this run serves a document of its own.
    stock = args.data / "policy.v0-prose.md"
    if stock.exists() and policy.read_text() != stock.read_text():
        raise SystemExit(
            f"REFUSING: {policy} is not the stock policy.\n"
            f"  An earlier arm left its own text there. Restore it before running,\n"
            f"  or this scores some other arm's prompt under this one's name."
        )

    task_ids = args.tasks or json.loads((args.data / "split.json").read_text())["test"]
    raw = json.loads((args.tau2 / f"data/tau2/domains/{args.domain}/tasks.json").read_text())
    raw = raw if isinstance(raw, list) else raw.get("tasks", [])
    tasks = [Task.model_validate(t) for t in raw if str(t["id"]) in set(map(str, task_ids))]
    if not tasks:
        raise SystemExit(f"no tasks matched {task_ids}")

    served = args.data / args.policy if args.policy else None
    if served and not served.exists():
        raise SystemExit(f"no such policy document: {served}")

    label = f"{served.name}, WHOLE DOCUMENT" if served else "STOCK POLICY"
    print(f"[arm b] {label} ({len((served or policy).read_text().split())} words), tau2's own agent")
    print(f"[arm b] tasks={[t.id for t in tasks]} trials={args.trials}")
    print(f"[arm b] executor={args.agent_llm}  user={args.user_llm}")
    if served:
        print("[arm b] the agent reads rules, gates and routing together; no gate is EVALUATED")
        print("[arm b] gates here are prose the agent may honour or ignore — nothing checks it")
    else:
        print("[arm b] no ASOP, no stepwise presentation, no gate — nothing to verify")

    try:
        if served:
            shutil.copy(served, policy)
        results = run_tasks(
            domain=args.domain,
            tasks=tasks,
            agent="llm_agent",
            user="user_simulator",
            llm_agent=args.agent_llm,
            llm_user=args.user_llm,
            num_trials=args.trials,
            max_steps=args.max_steps,
            max_concurrency=1,  # matched to arm C so latency is not a hidden variable
            evaluation_type=EvaluationType.ENV,
            save_to=str(args.out / "results.json"),
            console_display=False,
        )
    finally:
        # Same contract as arm C: a crash that leaves an ASOP at the canonical
        # policy path silently corrupts every later run, including somebody
        # else's who does not know this script exists.
        if served:
            shutil.copy(backup, policy)
            print("[arm b] original policy restored")

    sims = getattr(results, "simulations", []) or []
    # A simulation that died before evaluation carries reward_info=None. Scoring
    # it as 0 silently converts "the harness broke" into "the model failed", and
    # dropping it silently keeps only the runs lucky enough to survive — a biased
    # subsample that reads as a clean number. Both are worse than no number.
    scored = [s for s in sims if getattr(s, "reward_info", None) is not None]
    broken = [s for s in sims if getattr(s, "reward_info", None) is None]

    if broken:
        reasons = collections.Counter(str(getattr(s, "termination_reason", "?")) for s in broken)
        print(f"\n[arm b] REFUSING TO REPORT pass^1 — {len(broken)}/{len(sims)} simulations never scored")
        for reason, n in reasons.most_common():
            print(f"[arm b]   {n:>3}  {reason}")
        print("[arm b] `infrastructure_error` means the harness failed, not the model.")
        print("[arm b] Fix the cause and re-run; do not compare a partial arm to a full one.")
        if scored:
            r = [s.reward_info.reward for s in scored]
            print(f"[arm b] (survivors only, BIASED, not a result: {sum(r) / len(r):.3f} over {len(r)})")
        return 1

    # FAULT 2: the user granted consent and stopped in the same turn, so the agent
    # never got a turn in which to act. Reported, never silently counted as a failure.
    skipped = harness_fixes.unscoreable_cells(results)
    for c in skipped:
        print(f"[arm b] UNSCOREABLE task {c['task_id']} trial {c['trial']}: "
              f"user consented and stopped in one turn — {c['said']!r}")
    if skipped:
        print(f"[arm b] {len(skipped)} cell(s) unscoreable; pass^1 below EXCLUDES them")
    excluded = {(str(c["task_id"]), c["trial"]) for c in skipped}
    scored = [s for s in scored
              if (str(getattr(s, "task_id", None)), getattr(s, "trial", None)) not in excluded]

    rewards = [s.reward_info.reward for s in scored]
    if not rewards:
        print("\n[arm b] NO SIMULATIONS SCORED — check the log before reading anything into this")
        return 1
    print(f"\n[arm b] pass^1 = {sum(rewards) / len(rewards):.3f}  ({int(sum(rewards))}/{len(rewards)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
