#!/usr/bin/env python3
"""Run arm (c) — the ASOP executed stepwise, with gates — over a task subset.

    <tau2>/.venv/bin/python scripts/eval/run_arm_c.py \\
        --tau2 <checkout> --data evals/tau2-airline-asop \\
        --asop asops/asop.claude.md --out /tmp/armc --tasks 7 11 16

Registers `asop_stepwise` and calls tau2's own runner by name, so the borrowed
benchmark is never patched. The ASOP still reaches the agent by being the
domain policy file, exactly as arms B and C1-C3 did — which is what keeps the
document constant between (b) and (c), leaving the execution model as the only
difference.

WHY THE PER-STEP TABLE IS THE OUTPUT, not the score
---------------------------------------------------
A single-shot arm produces one number per conversation. Whatever it says, you
cannot act on it: "the run failed" names no step, so nothing can be adjudicated
and no version can be proposed.

Walking steps changes the unit of failure. A refusal lands on
`Cancel Flight step 4`, with a reason. Cluster those across tasks and a
procedure tells you which of its own steps is weakest — and THAT is a draft of
the next version. The pass rate is a side effect; the refusal table is the
product.
"""

from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path


def _load_adapter():
    src = Path(__file__).resolve().parent / "asop_agent.py"
    spec = importlib.util.spec_from_file_location("asop_agent", src)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["asop_agent"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tau2", type=Path, required=True)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--asop", type=str, default="asops/asop.claude.md")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--tasks", nargs="*", default=None, help="task ids; default = split TEST")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--agent-llm", default="openai/openai/gpt-oss-20b")
    ap.add_argument("--user-llm", default="openai/google/gemma-4-31b")
    ap.add_argument("--verifier-llm", default=None, help="defaults to the agent model")
    ap.add_argument("--max-steps", type=int, default=60)
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    verdict_log = args.out / "verdicts.jsonl"
    verdict_log.unlink(missing_ok=True)
    # tau2 offers to resume a run when its results file is already there, and
    # asks on stdin — which blocks forever under a pipe. Each invocation here
    # is a fresh run, and a half-written file from a crashed attempt would be
    # resumed rather than replaced.
    (args.out / "results.json").unlink(missing_ok=True)
    os.environ["ASOP_VERDICT_LOG"] = str(verdict_log)

    sys.path.insert(0, str(args.tau2 / "src"))
    aa = _load_adapter()

    from tau2.data_model.tasks import Task
    from tau2.registry import registry
    from tau2.run import run_tasks
    from tau2.evaluator.evaluator import EvaluationType

    verifier_llm = args.verifier_llm or args.agent_llm

    def factory(tools, domain_policy, **kwargs):
        kwargs.setdefault("verifier_llm", verifier_llm)
        return aa.create_asop_agent(tools, domain_policy, **kwargs)

    registry.register_agent_factory(factory, "asop_stepwise")

    # The ASOP reaches the agent as the domain policy, same as every other arm.
    # Restore it whatever happens: an extracted ASOP left at the canonical
    # policy path would silently corrupt every later run, including somebody
    # else's who does not know this script exists.
    policy = args.tau2 / "data/tau2/domains/airline/policy.md"
    backup = args.out / "policy.original.md"
    if not backup.exists():
        shutil.copy(policy, backup)

    task_ids = args.tasks or json.loads((args.data / "split.json").read_text())["test"]
    raw = json.loads((args.tau2 / "data/tau2/domains/airline/tasks.json").read_text())
    raw = raw if isinstance(raw, list) else raw.get("tasks", [])
    tasks = [Task.model_validate(t) for t in raw if str(t["id"]) in set(map(str, task_ids))]
    if not tasks:
        raise SystemExit(f"no tasks matched {task_ids}")

    print(f"[arm c] asop={args.asop} tasks={[t.id for t in tasks]} trials={args.trials}")
    print(f"[arm c] executor={args.agent_llm}  verifier={verifier_llm}")
    if verifier_llm == args.agent_llm:
        print("[arm c] NOTE: verifier shares the executor's weights — a weaker")
        print("[arm c]       check than a different model, and recorded as such.")

    try:
        shutil.copy(args.data / args.asop, policy)
        results = run_tasks(
            domain="airline",
            tasks=tasks,
            agent="asop_stepwise",
            user="user_simulator",
            llm_agent=args.agent_llm,
            llm_user=args.user_llm,
            num_trials=args.trials,
            max_steps=args.max_steps,
            max_concurrency=1,  # the sink correlates by conversation order
            evaluation_type=EvaluationType.ENV,
            save_to=str(args.out / "results.json"),
            console_display=False,
        )
    finally:
        shutil.copy(backup, policy)
        print("[arm c] original policy restored")

    return report(results, verdict_log, args.out)


def report(results, verdict_log: Path, out: Path) -> int:
    sims = getattr(results, "simulations", []) or []
    rewards = [s.reward_info.reward for s in sims]
    if rewards:
        print(f"\n[arm c] pass^1 = {sum(rewards) / len(rewards):.3f}  ({int(sum(rewards))}/{len(rewards)})")

    if not verdict_log.exists():
        print("[arm c] NO VERDICTS RECORDED — no gate ever fired. That is arm (b).")
        return 1

    verdicts = [json.loads(line) for line in verdict_log.read_text().splitlines() if line]
    per_step: dict[str, list[dict]] = collections.defaultdict(list)
    for v in verdicts:
        per_step[v["label"]].append(v)

    print(f"\n[arm c] {len(verdicts)} gate evaluations over {len(per_step)} distinct steps")
    print("\nWHERE THE PROCEDURE FAILS — the input to the next version")
    print(f"  {'step':<44}{'refused':>9}{'of':>5}  top reason")
    print("  " + "-" * 96)
    rows = sorted(
        per_step.items(),
        key=lambda kv: -sum(1 for v in kv[1] if not v["passed"]),
    )
    for label, vs in rows:
        bad = [v for v in vs if not v["passed"]]
        if not bad:
            continue
        reason = collections.Counter(v["reason"][:70] for v in bad).most_common(1)[0][0]
        print(f"  {label:<44}{len(bad):>9}{len(vs):>5}  {reason}")

    clean = [lbl for lbl, vs in per_step.items() if all(v["passed"] for v in vs)]
    print(f"\n  {len(clean)} step(s) never refused: {', '.join(sorted(clean)[:6])}")

    fell = sum(1 for v in verdicts if v["fell_back"])
    if fell:
        print(
            f"\n  {fell}/{len(verdicts)} gates declared 'deterministic' but named no "
            "re-runnable check, so they were judged instead."
        )

    summary = {
        "gate_evaluations": len(verdicts),
        "distinct_steps": len(per_step),
        "refusals": sum(1 for v in verdicts if not v["passed"]),
        "fell_back": fell,
        "pass_1": (sum(rewards) / len(rewards)) if rewards else None,
        "per_step": {
            lbl: {
                "evaluated": len(vs),
                "refused": sum(1 for v in vs if not v["passed"]),
                "reasons": collections.Counter(
                    v["reason"][:120] for v in vs if not v["passed"]
                ).most_common(3),
            }
            for lbl, vs in per_step.items()
        },
    }
    (out / "step_failures.json").write_text(json.dumps(summary, indent=2))
    print(f"\n[arm c] per-step failure data -> {out / 'step_failures.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
