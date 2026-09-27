#!/usr/bin/env python3
"""H4 -- local System-1 judge scored by answer-token probability (mlx-lm direct).

Why this exists: LM Studio's OpenAI-compatible API on this machine returns no
`logprobs` for MLX models, so `sopbench_judge.py --llm` (which reads sampled
text) cannot measure a probability. This script loads the MLX weights
directly with `mlx_lm` and reads next-token logits itself.

Data: the byte-identical 400-cell `hotel` sample sopbench_judge.py's own
`stratified(rows, 200, 20260923)` produces from
`scripts/eval/sopbench_extract.py --domain hotel --render v1` -- verified to
reproduce the same 400 ids, in the same order, as
evals/sopbench-hotel-asop/v1/sopbench_jev_jev-latest.json before any of this
was run (see H4 pre-registration in ai-tasks/local-s1s2/PLAN.md).

Prompt: same step/tool-history/transcript rendering as
scripts/eval/sopbench_judge.py's VERIFIER_PROMPT (same three fields, same
text). The tail instruction is REWRITTEN to ask for a single word ("yes"/
"no") instead of a "PASS - <reason>" line, because a first-token-probability
method needs the label to BE the first generated token. That is a documented,
necessary deviation, not a drift -- the question asked ("does the evidence
show the prerequisites were satisfied") is unchanged.

Harmony format (gpt-oss): the chat template's `add_generation_prompt` only
emits `<|start|>assistant`; the model then free-chooses a channel
(analysis/commentary/final). Two variants:

  --variant none   force the NEXT tokens straight into the final channel by
                   appending `<|channel|>final<|message|>` ourselves before
                   any generation -- the true System-1 candidate (single
                   prefill, one forward pass, no sampling).
  --variant bounded let the model generate up to --budget tokens of
                   reasoning (reasoning_effort=low) with ordinary greedy
                   decoding, THEN force the same
                   `<|end|><|start|>assistant<|channel|>final<|message|>`
                   boundary and read logits there -- scored identically to
                   --variant none, just after a reasoning prefix.

p(label) = softmax restricted to the two single-token label ids (yes=6763,
no=1750 for this tokenizer -- verified in-script, never assumed), per the
task's own spec ("softmax over the label tokens' first-token logits").
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import mlx.core as mx
from mlx_lm.generate import generate_step
from mlx_lm.models.cache import make_prompt_cache
from mlx_lm.tokenizer_utils import load as load_tokenizer
from mlx_lm.utils import load as load_model

REPO = Path.home() / "Code" / "agentco-harness"
sys.path.insert(0, str(REPO / "scripts" / "eval"))
from sopbench_judge import stratified  # noqa: E402

MODEL_DIR = Path.home() / ".lmstudio" / "models" / "mlx-community" / "gpt-oss-20b-MXFP4-Q8"

QUESTION = """\
You are a verifier. You did not do this work and you are not helping anyone \
finish it.

An action was attempted. Decide ONE thing: does the visible evidence show the \
action's stated prerequisites were satisfied?

THE ACTION AND ITS PREREQUISITES
{step}

EVERY TOOL CALL MADE SO FAR, WITH RESULTS
{tools}

THE CONVERSATION SO FAR
{evidence}

Answer with exactly one word and nothing else: "yes" if every required \
prerequisite was satisfied by a specific tool call and result you can point \
to, or "no" if any required prerequisite call is absent, was called with \
different parameters, or did not succeed. A tool call merely having been \
made is not evidence that its result satisfied anything."""


def build_question(row: dict) -> str:
    ev = row["evidence"]
    return QUESTION.format(
        step=ev["step_body"],
        tools="\n".join(ev["tool_history"]) or "(no tool call has been made)",
        evidence="\n".join(ev["transcript"]) or "(no turns yet)",
    )


def load_hotel_400() -> list[dict]:
    dec_path = Path("/tmp/h4-work/hotel_decisions.jsonl")
    rows = [json.loads(l) for l in open(dec_path) if l.strip()]
    sample = stratified(rows, 200, 20260923)
    ref = json.loads(
        (REPO / "evals/sopbench-hotel-asop/v1/sopbench_jev_jev-latest.json").read_text()
    )
    ref_ids = [d["id"] for d in ref["decisions"]]
    assert [r["_id"] for r in sample] == ref_ids, "sample does not match Jev's 400-cell ids"
    return sample


def label_ids(tok) -> tuple[int, int]:
    yes_ids = tok.encode("yes", add_special_tokens=False)
    no_ids = tok.encode("no", add_special_tokens=False)
    assert len(yes_ids) == 1 and len(no_ids) == 1, (yes_ids, no_ids)
    return yes_ids[0], no_ids[0]


def prefix_ids(tok, row: dict, reasoning_effort: str = "low") -> list[int]:
    msgs = [{"role": "user", "content": build_question(row)}]
    text = tok.apply_chat_template(
        msgs, add_generation_prompt=True, tokenize=False,
        reasoning_effort=reasoning_effort,
    )
    return tok.encode(text, add_special_tokens=False)


FINAL_HEADER = "<|channel|>final<|message|>"
CLOSE_AND_FINAL = "<|end|><|start|>assistant<|channel|>final<|message|>"
# <|end|> closes a channel message; <|return|> is the eos that ends generation
# entirely. Either means the model considers itself done reasoning -- stop the
# free-running budget there rather than past it.
STOP_IDS = {200002, 200007}


def p_yes_no(logprobs, yes_id: int, no_id: int) -> tuple[float, float]:
    """Restricted softmax over just the two label ids (per the pre-registered
    method), not the full-vocab probability -- `logprobs` is already a
    log-softmax over the whole vocab, so this renormalizes onto {yes, no}."""
    ly, ln = float(logprobs[yes_id]), float(logprobs[no_id])
    m = max(ly, ln)
    ey, en = pow(2.718281828459045, ly - m), pow(2.718281828459045, ln - m)
    return ey / (ey + en), en / (ey + en)


def _first_logprobs(model, ids: list[int], prompt_cache) -> "mx.array":
    """Prefill `ids` into `prompt_cache` (KV-cached, correct for gpt-oss's
    sliding/full attention layer mix) and return the log-softmax vector for
    the token that would follow."""
    for tok_id, logprobs in generate_step(
        mx.array(ids), model, max_tokens=1, prompt_cache=prompt_cache
    ):
        return logprobs
    raise RuntimeError("generate_step yielded nothing")


def judge_none(model, tok, row: dict, yes_id: int, no_id: int) -> dict:
    t0 = time.time()
    ids = prefix_ids(tok, row, reasoning_effort="low")
    ids = ids + tok.encode(FINAL_HEADER, add_special_tokens=False)
    cache = make_prompt_cache(model)
    logprobs = _first_logprobs(model, ids, cache)
    p_yes, p_no = p_yes_no(logprobs, yes_id, no_id)
    dt = time.time() - t0
    return _row_result(row, p_yes, p_no, dt, n_reasoning_tokens=0)


def judge_bounded(model, tok, row: dict, yes_id: int, no_id: int, budget: int) -> dict:
    t0 = time.time()
    ids = prefix_ids(tok, row, reasoning_effort="low")
    cache = make_prompt_cache(model)
    generated: list[int] = []
    for tok_id, _lp in generate_step(
        mx.array(ids), model, max_tokens=budget, prompt_cache=cache
    ):
        if tok_id in STOP_IDS:
            break
        generated.append(tok_id)
    close_ids = tok.encode(CLOSE_AND_FINAL, add_special_tokens=False)
    logprobs = _first_logprobs(model, close_ids, cache)
    p_yes, p_no = p_yes_no(logprobs, yes_id, no_id)
    dt = time.time() - t0
    return _row_result(row, p_yes, p_no, dt, n_reasoning_tokens=len(generated))


def _row_result(row: dict, p_yes: float, p_no: float, dt: float, n_reasoning_tokens: int) -> dict:
    passed = p_yes > p_no  # True = held/PASS predicted, False = not_held/FAIL predicted
    confidence = max(p_yes, p_no)
    return {
        "id": row["_id"], "truth": row["truth"], "passed": passed,
        "confidence": confidence, "p_yes": p_yes, "p_no": p_no,
        "latency_s": dt, "n_reasoning_tokens": n_reasoning_tokens,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=["none", "bounded"], required=True)
    ap.add_argument("--budget", type=int, default=200)
    ap.add_argument("--limit", type=int, default=0, help="0 = all 400")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sanity", action="store_true",
                    help="also print N sampled tokens after the forced final "
                         "header, to eyeball that forcing didn't break the model")
    args = ap.parse_args()

    print(f"loading tokenizer + model from {MODEL_DIR} ...", flush=True)
    tok = load_tokenizer(MODEL_DIR)
    model, _ = load_model(str(MODEL_DIR))
    yes_id, no_id = label_ids(tok)
    print(f"label ids: yes={yes_id} no={no_id}", flush=True)

    rows = load_hotel_400()
    if args.limit:
        rows = rows[: args.limit]
    print(f"scoring {len(rows)} rows, variant={args.variant}", flush=True)

    results = []
    t0 = time.time()
    for i, row in enumerate(rows):
        if args.variant == "none":
            r = judge_none(model, tok, row, yes_id, no_id)
        else:
            r = judge_bounded(model, tok, row, yes_id, no_id, args.budget)
        results.append(r)
        if args.sanity:
            ids = prefix_ids(tok, row, reasoning_effort="low")
            ids = ids + tok.encode(FINAL_HEADER, add_special_tokens=False)
            sample_cache = make_prompt_cache(model)
            sample_ids = []
            nxt_ids = mx.array(ids)
            for _ in range(12):
                for tok_id, _lp in generate_step(
                    nxt_ids, model, max_tokens=1, prompt_cache=sample_cache
                ):
                    sample_ids.append(tok_id)
                    nxt_ids = mx.array([tok_id])
                if sample_ids[-1] in STOP_IDS:
                    break
            print(f"  [{row['_id']}] truth={row['truth']} p_yes={r['p_yes']:.3f} "
                  f"p_no={r['p_no']:.3f} sampled_after_final="
                  f"{tok.decode(sample_ids)!r}", flush=True)
        if (i + 1) % 20 == 0:
            rate = (time.time() - t0) / (i + 1)
            print(f"  {i+1}/{len(rows)}  ({rate:.2f}s/row, "
                  f"~{rate*(len(rows)-i-1)/60:.1f}min left)", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "model": "mlx-community/gpt-oss-20b-MXFP4-Q8",
        "variant": args.variant, "budget": args.budget if args.variant == "bounded" else None,
        "n": len(results), "decisions": results,
    }, indent=2))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
