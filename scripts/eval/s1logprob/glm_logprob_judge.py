#!/usr/bin/env python3
"""H4b (a) -- GLM-4.7-Flash MLX 8-bit as a local S1 judge, scored by answer-
token probability. Same method, same 400-cell hotel sample, same question as
s1_logprob_judge.py's gpt-oss-20b run -- only the forcing mechanism differs,
because GLM's own chat template already has a built-in no-think switch
(`enable_thinking=False` renders `<|assistant|></think>`, i.e. the template
itself forces straight to content) instead of harmony's explicit channel
tags. Reuses `load_hotel_400` and `build_question` from s1_logprob_judge.py
so the prompt text is identical to the gpt-oss-20b arm, not re-derived.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import mlx.core as mx
from mlx_lm.generate import generate_step
from mlx_lm.models.cache import make_prompt_cache
from mlx_lm.tokenizer_utils import load as load_tokenizer
from mlx_lm.utils import load as load_model

from s1_logprob_judge import build_question, load_hotel_400, _row_result

MODEL_DIR = Path.home() / ".lmstudio" / "models" / "lmstudio-community" / "GLM-4.7-Flash-MLX-8bit"
THINK_CLOSE = "</think>"
MIN_FREE_PCT = 15


def free_mem_pct() -> int | None:
    """Self-protecting guard: don't rely on an external watcher's reaction
    time (2026-09-26 incident -- free memory dropped to 12% mid-run while
    being watched from outside and had to be killed by hand)."""
    try:
        out = subprocess.run(["memory_pressure"], capture_output=True, text=True,
                              timeout=5).stdout
        m = re.search(r"free percentage:\s*(\d+)%", out)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def label_ids(tok) -> tuple[int, int]:
    yes_ids = tok.encode("yes", add_special_tokens=False)
    no_ids = tok.encode("no", add_special_tokens=False)
    assert len(yes_ids) == 1 and len(no_ids) == 1, (yes_ids, no_ids)
    return yes_ids[0], no_ids[0]


def p_yes_no(logprobs, yes_id: int, no_id: int) -> tuple[float, float]:
    ly, ln = float(logprobs[yes_id]), float(logprobs[no_id])
    m = max(ly, ln)
    ey, en = pow(2.718281828459045, ly - m), pow(2.718281828459045, ln - m)
    return ey / (ey + en), en / (ey + en)


def _first_logprobs(model, ids: list[int], prompt_cache) -> "mx.array":
    for tok_id, logprobs in generate_step(
        mx.array(ids), model, max_tokens=1, prompt_cache=prompt_cache
    ):
        return logprobs
    raise RuntimeError("generate_step yielded nothing")


def judge_none(model, tok, row: dict, yes_id: int, no_id: int) -> dict:
    t0 = time.time()
    msgs = [{"role": "user", "content": build_question(row)}]
    text = tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False,
                                    enable_thinking=False)
    ids = tok.encode(text, add_special_tokens=False)
    cache = make_prompt_cache(model)
    logprobs = _first_logprobs(model, ids, cache)
    p_yes, p_no = p_yes_no(logprobs, yes_id, no_id)
    dt = time.time() - t0
    return _row_result(row, p_yes, p_no, dt, n_reasoning_tokens=0)


def judge_bounded(model, tok, row: dict, yes_id: int, no_id: int, budget: int,
                   think_close_id: int) -> dict:
    """Let the model reason inside <think>...</think> for up to `budget`
    tokens, then read the label logits for whatever comes right after.

    `generate_step`'s (token, logprobs) pairs are causally offset: the
    logprobs yielded alongside a token are the distribution that PRODUCED
    that token, i.e. P(token | preceding context) -- and by the time a pair
    is yielded, that token is already committed into `prompt_cache` (traced
    in generate_step's own loop: `_step(y)` both advances the cache and
    computes the following pair before the current one is yielded). So the
    pair for "the label that follows </think>" is just the NEXT pair the
    same generator produces once </think> has been yielded -- `next(gen)`
    resumes that exact generator against the same cache, no re-encoding.
    """
    t0 = time.time()
    msgs = [{"role": "user", "content": build_question(row)}]
    text = tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False)
    ids = tok.encode(text, add_special_tokens=False)
    cache = make_prompt_cache(model)
    gen = generate_step(mx.array(ids), model, max_tokens=budget, prompt_cache=cache)
    generated: list[int] = []
    closed_naturally = False
    for tok_id, _lp in gen:
        generated.append(tok_id)
        if tok_id == think_close_id:
            closed_naturally = True
            break
    if closed_naturally:
        try:
            _next_tok, logprobs = next(gen)
        except StopIteration:
            closed_naturally = False  # budget exhausted exactly on </think>
    if not closed_naturally:
        close_ids = tok.encode(THINK_CLOSE, add_special_tokens=False)
        logprobs = _first_logprobs(model, close_ids, cache)
    p_yes, p_no = p_yes_no(logprobs, yes_id, no_id)
    dt = time.time() - t0
    return _row_result(row, p_yes, p_no, dt,
                        n_reasoning_tokens=len(generated) - (1 if closed_naturally else 0))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=["none", "bounded"], required=True)
    ap.add_argument("--budget", type=int, default=200)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sanity", action="store_true")
    args = ap.parse_args()

    print(f"loading tokenizer + model from {MODEL_DIR} ...", flush=True)
    tok = load_tokenizer(MODEL_DIR)
    model, _ = load_model(str(MODEL_DIR))
    yes_id, no_id = label_ids(tok)
    think_close_ids = tok.encode(THINK_CLOSE, add_special_tokens=False)
    assert len(think_close_ids) == 1
    think_close_id = think_close_ids[0]
    print(f"label ids: yes={yes_id} no={no_id} think_close={think_close_id}", flush=True)

    rows = load_hotel_400()
    if args.limit:
        rows = rows[: args.limit]
    print(f"scoring {len(rows)} rows, variant={args.variant}", flush=True)

    results = []
    aborted = False
    t0 = time.time()
    for i, row in enumerate(rows):
        pct = free_mem_pct()
        if pct is not None and pct < MIN_FREE_PCT:
            print(f"[ABORT] free memory {pct}% < {MIN_FREE_PCT}% floor at row {i}/{len(rows)} "
                  f"-- stopping and writing partial results", flush=True)
            aborted = True
            break
        if args.variant == "none":
            r = judge_none(model, tok, row, yes_id, no_id)
        else:
            r = judge_bounded(model, tok, row, yes_id, no_id, args.budget, think_close_id)
        results.append(r)
        if args.sanity:
            print(f"  [{row['_id']}] truth={row['truth']} p_yes={r['p_yes']:.3f} "
                  f"p_no={r['p_no']:.3f} n_reasoning={r['n_reasoning_tokens']}", flush=True)
        if (i + 1) % 20 == 0:
            rate = (time.time() - t0) / (i + 1)
            print(f"  {i+1}/{len(rows)}  ({rate:.2f}s/row, "
                  f"~{rate*(len(rows)-i-1)/60:.1f}min left, mem_free={pct}%)", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "model": "lmstudio-community/GLM-4.7-Flash-MLX-8bit",
        "variant": args.variant, "budget": args.budget if args.variant == "bounded" else None,
        "n": len(results), "aborted_low_memory": aborted, "decisions": results,
    }, indent=2))
    print(f"wrote {args.out}")
    if aborted:
        sys.exit(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
