#!/usr/bin/env python3
"""Run the ASOP evolution loop: execute, adjudicate, propose, re-run.

    <tau2>/.venv/bin/python scripts/eval/asop_rounds.py \\
        --tau2 <checkout> --data evals/tau2-airline-asop \\
        --out /tmp/rounds --asop asops/asop.claude.md \\
        --tasks 16 11 32 33 --rounds 3

WHAT THIS MEASURES, and why it is not the earlier experiment
------------------------------------------------------------
The 2026-09-12 run asked whether an ASOP-shaped prompt beats a prose one on a
single execution. Null, and the design could not have answered it anyway.

This asks the question the format actually makes possible: **take a procedure
that already exists, run it, and see how much better it gets after one, two,
three rounds of being run.** The claim under test is not "ASOPs execute better"
— it is "ASOPs EVOLVE, and the evolution is driven by evidence the execution
itself produced".

That reframes the weak local model from a limitation into the point. A model
that fails often generates many localised failures, and localised failures are
the raw material. What matters is the slope across rounds, not the intercept.

WHY THIS CANNOT WORK WITHOUT STEPWISE EXECUTION
-----------------------------------------------
A single-shot run yields one number per conversation. "The run failed" names no
step, so there is nothing to adjudicate and nothing to revise. Walking steps
moves the unit of failure onto a step with a reason — and a step with a reason
is a draft of the next version. The gate is what makes the loop possible at
all; the score is a by-product.

THE TWO RULES THAT KEEP IT HONEST
----------------------------------
1. The proposer never sees a task, its gold actions, or the database. It sees
   the step it is revising and the refusal reasons that step accumulated.
   Anything else and later versions are tuned to the answer key.
2. TEST is never the thing you improve on. Adjudicate on DEV, measure on TEST,
   or the curve measures memorisation and calls it evolution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent

PROPOSE_PROMPT = """\
You are revising ONE step of an operating procedure.

You have not seen the tasks this procedure is run against and you must not
guess at them. Write for the domain, not for a scenario.

THE STEP AS IT STANDS
{step}

WHAT WENT WRONG WHEN IT RAN
This step was evaluated {evaluated} times and refused {refused} times. The \
verifier gave these reasons:
{reasons}

Rewrite the step so those refusals stop happening, WITHOUT changing what the
procedure requires. You may make an implicit precondition explicit, split a
conflated instruction, state an ordering the step assumed, or name a
prohibition the step relied on the reader to infer. You may NOT add a new
rule, relax an existing one, or resolve an ambiguity the source left open.

Output the revised step as markdown, in the same shape as the original,
and nothing else. No commentary.\
"""


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def run_round(args, asop_rel: str, out: Path) -> dict:
    """One execution round. Returns the outcome row for the ledger."""
    cmd = [
        sys.executable, str(HERE / "run_arm_c.py"),
        "--tau2", str(args.tau2), "--data", str(args.data),
        "--asop", asop_rel, "--out", str(out),
        "--trials", str(args.trials), "--max-steps", str(args.max_steps),
        "--agent-llm", args.agent_llm, "--user-llm", args.user_llm,
    ]
    if args.verifier_llm:
        cmd += ["--verifier-llm", args.verifier_llm]
    if args.tasks:
        cmd += ["--tasks", *args.tasks]
    print(f"  running: {asop_rel} -> {out}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    (out / "run.log").write_text(proc.stdout + proc.stderr)

    failures = out / "step_failures.json"
    if not failures.exists():
        print(f"  ROUND FAILED — no step data. See {out / 'run.log'}")
        return {"error": "no step_failures.json", "log": str(out / "run.log")}
    data = json.loads(failures.read_text())
    return {
        "pass_1": data.get("pass_1"),
        "gate_evaluations": data["gate_evaluations"],
        "refusals": data["refusals"],
        "distinct_steps": data["distinct_steps"],
        "fell_back": data["fell_back"],
        "per_step": data["per_step"],
    }


def worst_steps(row: dict, limit: int) -> list[tuple[str, dict]]:
    """The steps carrying the most refusals — the adjudication queue."""
    items = [(k, v) for k, v in row.get("per_step", {}).items() if v["refused"]]
    return sorted(items, key=lambda kv: -kv[1]["refused"])[:limit]


def propose(asop_text: str, label: str, stats: dict, judge) -> tuple[str, str] | None:
    """Adjudicate one step's refusals into a revised step.

    Returns (old_step_text, new_step_text), or None when the step cannot be
    located in the document — which is reported rather than silently skipped,
    because a proposal that quietly applied to nothing would show up as "the
    loop ran and nothing improved".
    """
    proc, _, num = label.partition(" · step ")
    num = num.strip()
    # Find the numbered step under its procedure heading.
    m = re.search(
        rf"^##\s+(?:Procedure[:\s][^\n]*)?{re.escape(proc)}[^\n]*$(.*?)(?=^##\s|\Z)",
        asop_text,
        re.M | re.S,
    )
    if not m:
        return None
    body = m.group(1)
    sm = re.search(rf"^{num}\.\s+(.*?)(?=^\d+\.\s|\Z)", body, re.M | re.S)
    if not sm:
        return None
    old = sm.group(0)

    reasons = "\n".join(f"  - {r} (x{n})" for r, n in stats["reasons"])
    revised = judge(
        PROPOSE_PROMPT.format(
            step=old.strip(),
            evaluated=stats["evaluated"],
            refused=stats["refused"],
            reasons=reasons or "  (none recorded)",
        )
    )
    revised = revised.strip()
    if not revised or revised == old.strip():
        return None
    if not revised.startswith(f"{num}."):
        revised = f"{num}. {revised.lstrip('0123456789. ')}"
    return old, revised + "\n\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tau2", type=Path, required=True)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--asop", default="asops/asop.claude.md")
    ap.add_argument("--tasks", nargs="*", default=None)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--revise-per-round", type=int, default=2)
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--agent-llm", default="openai/openai/gpt-oss-20b")
    ap.add_argument("--user-llm", default="openai/google/gemma-4-31b")
    ap.add_argument("--verifier-llm", default=None)
    ap.add_argument("--proposer-llm", default=None, help="defaults to the agent model")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(args.tau2 / "src"))
    from tau2.data_model.message import SystemMessage
    from tau2.utils.llm_utils import generate

    proposer_llm = args.proposer_llm or args.agent_llm

    def judge(prompt: str) -> str:
        reply = generate(
            model=proposer_llm,
            tools=[],
            messages=[SystemMessage(role="system", content=prompt)],
            call_name="asop_propose",
        )
        return str(getattr(reply, "content", "") or "")

    ledger: list[dict] = []
    current_rel = args.asop
    versions_dir = args.data / "asops"

    for r in range(1, args.rounds + 1):
        text = (args.data / current_rel).read_text()
        print(f"\n=== round {r}: {current_rel} (sha {sha(text)}, {len(text.split())} words)")
        row = run_round(args, current_rel, args.out / f"round{r}")
        row.update(
            {
                "round": r,
                "asop": current_rel,
                "asop_sha": sha(text),
                "words": len(text.split()),
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
        ledger.append(row)
        (args.out / "ledger.json").write_text(json.dumps(ledger, indent=2))

        if "error" in row:
            print("  stopping: the round produced no data to adjudicate")
            break
        print(
            f"  pass^1={row['pass_1']}  refusals={row['refusals']}"
            f"/{row['gate_evaluations']} over {row['distinct_steps']} steps"
        )

        if r == args.rounds:
            break

        queue = worst_steps(row, args.revise_per_round)
        if not queue:
            print("  nothing refused — no proposal to make, and no next version")
            break

        revised = text
        applied = []
        for label, stats in queue:
            got = propose(revised, label, stats, judge)
            if got is None:
                print(f"  ! could not locate or revise {label}; left unchanged")
                continue
            old, new = got
            revised = revised.replace(old, new, 1)
            applied.append(label)
            print(f"  proposed a revision to {label} ({stats['refused']} refusals)")

        if not applied:
            print("  no proposal applied — stopping rather than re-running the same document")
            break

        nxt = f"asops/asop.round{r + 1}.md"
        header = (
            f"<!-- generated: round {r + 1}, from {current_rel} (sha {sha(text)})\n"
            f"     revised steps: {', '.join(applied)}\n"
            f"     proposer: {proposer_llm}\n"
            f"     source of the revisions: refusal reasons recorded in round {r}. "
            f"No task, gold action or database state was visible to the proposer. -->\n\n"
        )
        (args.data / nxt).write_text(header + revised)
        current_rel = nxt

    print("\n=== outcomes per version ===")
    print(f"  {'round':<7}{'asop':<30}{'pass^1':>8}{'refused':>9}{'of':>5}")
    for row in ledger:
        if "error" in row:
            print(f"  {row['round']:<7}{row['asop']:<30}{'ERROR':>8}")
            continue
        print(
            f"  {row['round']:<7}{row['asop']:<30}"
            f"{(row['pass_1'] if row['pass_1'] is not None else -1):>8.3f}"
            f"{row['refusals']:>9}{row['gate_evaluations']:>5}"
        )
    print(f"\nledger -> {args.out / 'ledger.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
