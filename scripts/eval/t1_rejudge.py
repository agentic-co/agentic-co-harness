#!/usr/bin/env python3
"""Re-judge recorded gate decisions with a different model.

    <tau2>/.venv/bin/python scripts/eval/t1_rejudge.py \\
        --tau2 <checkout> --worksheet t1_ws_full.jsonl \\
        --models openai/openai/gpt-oss-20b openai/qwen/qwen3.6-27b

THE POINT
---------
T1 found that the gate refuses LESS often on preconditions that demonstrably
failed (0.48) than it does on average (0.54). A detector does the opposite.

That leaves two very different explanations, and they matter enormously:

  (a) gates on judged business rules cannot discriminate, full stop;
  (b) a 20B model judging its own kind of reasoning cannot discriminate,
      and a better judge would.

The whole ASOP verification claim rides on which one it is, and nothing in the
original run separates them because the executor and the verifier were the same
weights.

WHY REPLAY RATHER THAN RE-RUN
-----------------------------
Every recorded decision carries the evidence the verifier saw. So the judge can
be swapped while holding EVERYTHING else identical — same transcripts, same
tool histories, same prompts, same deterministic labels. Re-running the agent
would change the transcripts too, and any difference would be uninterpretable.

It is also about thirty times cheaper: one call per decision, no agent, no user
simulator, no environment.

WHAT THIS CANNOT SHOW
---------------------
A better verifier scoring better here does NOT mean the gate would have helped
the run. These decisions were made in conversations the new judge never
influenced; a verifier that would have refused earlier might have changed
everything downstream. This measures judgment quality on fixed evidence and
nothing else, which is exactly C1 and is not C2 or C3.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


def load_adapter(tau2: Path):
    sys.path.insert(0, str(tau2 / "src"))
    src = Path(__file__).resolve().parent / "asop_agent.py"
    spec = importlib.util.spec_from_file_location("asop_agent", src)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["asop_agent"] = mod
    spec.loader.exec_module(mod)
    return mod


def rebuild_prompt(aa, row: dict) -> tuple[str, bool]:
    """The exact prompt the original verifier received for this decision."""
    ev = row["evidence"]
    conditional = aa.is_conditional(ev["step_body"])
    na_option = (
        "\nor\nN/A — <why this step does not apply to this request>"
        if conditional
        else ""
    )
    return (
        aa.VERIFIER_PROMPT.format(
            na_option=na_option,
            step=f"{row['label']}: {ev['step_body']}",
            tools="\n".join(ev["tool_history"]) or "(no tool call has been made)",
            evidence="\n".join(ev["transcript"]) or "(no turns yet)",
        ),
        conditional,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tau2", type=Path, required=True)
    ap.add_argument("--worksheet", type=Path, required=True)
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--limit", type=int, default=0, help="0 = every labelled decision")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    aa = load_adapter(args.tau2)
    from tau2.data_model.message import SystemMessage
    from tau2.utils.llm_utils import generate

    lines = [l for l in args.worksheet.read_text().splitlines() if l.strip()]
    rows = [json.loads(l) for l in lines[1:]]
    # Judge EVERY decision with evidence, not only the labelled ones. Recall on
    # failed preconditions is meaningless alone — a judge that refuses
    # everything scores 1.00. The base refusal rate over all decisions is the
    # control, and the LIFT between them is the only number here that can tell
    # discrimination from a reflex.
    rows = [r for r in rows if "evidence" in r]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        raise SystemExit("no labelled decisions with evidence in that worksheet")

    n_unmet = sum(1 for r in rows if r["truth"] == "not_held")
    print(f"[rejudge] {len(rows)} decisions ({n_unmet} with a precondition that failed)")
    print(f"[rejudge] replaying identical evidence through {len(args.models)} judge(s)\n")

    table: list[dict] = []
    for model in args.models:
        refusals = hits = errors = 0
        per_row = []
        for i, r in enumerate(rows, 1):
            prompt, conditional = rebuild_prompt(aa, r)
            try:
                reply = generate(
                    model=model,
                    tools=[],
                    messages=[SystemMessage(role="system", content=prompt)],
                    call_name="t1_rejudge",
                )
                passed, reason, na = aa.parse_verdict(
                    str(getattr(reply, "content", "") or ""), conditional
                )
            except Exception as exc:  # a judge that errors has not judged
                errors += 1
                passed, reason, na = True, f"judge error: {exc}"[:120], False
            if na:
                continue
            if not passed:
                refusals += 1
                if r.get("truth") == "not_held":
                    hits += 1
            per_row.append(
                {"id": r.get("_id"), "passed": passed, "truth": r.get("truth"), "reason": reason}
            )
            if i % 25 == 0:
                print(f"    {model}: {i}/{len(rows)}")

        judged = len(per_row)
        base = refusals / judged if judged else None
        recall = hits / n_unmet if n_unmet else None
        row = {
            "model": model,
            "judged": judged,
            "errors": errors,
            "refusal_rate": base,
            "recall_on_failed": recall,
            "lift": (recall - base) if (base is not None and recall is not None) else None,
        }
        table.append(row)
        if args.out:
            (args.out / f"rejudge_{model.replace('/', '_')}.json").write_text(
                json.dumps({"summary": row, "decisions": per_row}, indent=2)
            )

    print(f"\n{'judge':<32}{'judged':>7}{'err':>5}{'refuses':>9}{'catches':>9}{'LIFT':>8}")
    print("-" * 73)
    for r in table:
        def f(k):
            return f"{r[k]:+.2f}" if k == "lift" and r[k] is not None else (
                f"{r[k]:.2f}" if r[k] is not None else "  — "
            )
        print(
            f"{r['model']:<32}{r['judged']:>7}{r['errors']:>5}"
            f"{f('refusal_rate'):>9}{f('recall_on_failed'):>9}{f('lift'):>8}"
        )

    print(
        "\nrefuses = how often this judge refused anything at all.\n"
        "catches = how often it refused where a deterministic rule shows the\n"
        "          precondition FAILED.\n"
        "LIFT    = catches minus refuses. This is the whole measurement.\n\n"
        "A judge that refuses at random scores LIFT 0 however high its catch rate\n"
        "looks — refusing everything gives catches 1.00 and lift 0. Positive lift\n"
        "is discrimination. The 20B verifier in the original run scored NEGATIVE\n"
        "lift: it refused less where the precondition had actually failed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
