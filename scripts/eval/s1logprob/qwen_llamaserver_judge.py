#!/usr/bin/env python3
"""H4b (b) -- Qwen3.6-35B-A3B (GGUF-only on this machine) as a local S1
judge, scored via llama.cpp's own `llama-server` (NOT LM Studio's :4242
server -- that one strips logprobs; this binary is LM Studio's bundled
llama.cpp build, run standalone) and its raw `/completion` endpoint.

Method matches s1_logprob_judge.py / glm_logprob_judge.py: same 400-cell
hotel sample, same question text (`build_question`), same two variants
(`none` = force straight past thinking, `bounded` = reasoning budget then
force the label). The only structural difference is HOW p(label) is
obtained: `/completion` with `n_probs=50` returns `top_logprobs`, the raw
pre-sampling log-softmax value for the top-50 candidate tokens at that
position; this script picks out the yes/no ids (9405/2083, verified against
this GGUF's own vocab via `/tokenize`) and restricts+renormalizes exactly
like the MLX arms do by hand. A GBNF grammar (`root ::= "yes" | "no"`) was
tried first and dropped: with `post_sampling_probs=True` this llama.cpp
build (2.41.0) collapses `top_probs` to the single already-sampled token at
prob=1.0 regardless of how close the runner-up was -- verified empirically
on hand-built contrast prompts -- so it cannot report a real margin.

Qwen3.6's own GGUF chat template (extracted from the model's
`tokenizer.chat_template` metadata field, saved to qwen36_chat_template.jinja
-- llama-server's binary vocab/architecture predates a Transformers-style
`apply_chat_template` in this venv, so it's rendered here with jinja2
directly) has the identical Qwen3-family thinking switch as gpt-oss/GLM:
`enable_thinking=False` renders `<think>\n\n</think>\n\n` (pre-closed, empty
-- straight to content); the default renders `<think>\n` and expects the
model to close it itself.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import jinja2

sys.path.insert(0, str(Path(__file__).parent))
from s1_logprob_judge import build_question, load_hotel_400  # noqa: E402

TEMPLATE_PATH = Path(__file__).parent / "qwen36_chat_template.jinja"
MIN_FREE_PCT = 15
THINK_CLOSE_SUFFIX = "\n</think>\n\n"
N_PROBS = 50
YES_ID, NO_ID = 9405, 2083  # verified via /tokenize against this exact GGUF's vocab


def free_mem_pct() -> int | None:
    try:
        out = subprocess.run(["memory_pressure"], capture_output=True, text=True,
                              timeout=5).stdout
        m = re.search(r"free percentage:\s*(\d+)%", out)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _raise_exception(msg: str):
    raise Exception(msg)


def make_renderer():
    env = jinja2.Environment(trim_blocks=True, lstrip_blocks=True)
    env.filters["tojson"] = lambda v, **kw: json.dumps(v)
    env.globals["raise_exception"] = _raise_exception
    return env.from_string(TEMPLATE_PATH.read_text())


def render_prompt(renderer, question: str, enable_thinking: bool) -> str:
    msgs = [{"role": "user", "content": question}]
    kwargs = dict(messages=msgs, add_generation_prompt=True, tools=None, add_vision_id=False)
    if not enable_thinking:
        kwargs["enable_thinking"] = False
    return renderer.render(**kwargs)


class LlamaServer:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def _post(self, path: str, payload: dict, timeout: int = 120) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=timeout))

    def complete(self, prompt: str, n_predict: int,
                 stop: list[str] | None = None, n_probs: int = 0) -> dict:
        payload = {
            "prompt": prompt, "n_predict": n_predict, "temperature": 0,
            "cache_prompt": True,
        }
        if stop:
            payload["stop"] = stop
        if n_probs:
            payload["n_probs"] = n_probs  # raw pre-sampling logprobs, NOT
            # post_sampling_probs=True: that field only reports the single
            # already-sampled token at prob=1.0 in this llama.cpp build
            # (verified empirically -- a grammar-constrained or greedy
            # completion collapses top_probs to one entry), which is useless
            # for a restricted-softmax read. The plain `n_probs` path returns
            # `top_logprobs`, the real top-N pre-sampling log-softmax values.
        return self._post("/completion", payload)


def p_yes_no_from_probs(resp: dict) -> tuple[float, float]:
    """`completion_probabilities[0].top_logprobs` lists the top `n_probs`
    candidates at the first generated position as raw (pre-sampling)
    log-softmax values -- restrict to the two label ids and renormalize,
    identical to the MLX arms' method."""
    cp = resp.get("completion_probabilities") or []
    if not cp:
        raise RuntimeError(f"no completion_probabilities in response: {resp}")
    top = cp[0].get("top_logprobs") or []
    d = {p["id"]: p["logprob"] for p in top}
    if YES_ID not in d or NO_ID not in d:
        raise RuntimeError(
            f"yes/no not both in top-{N_PROBS} logprobs (ids {YES_ID}/{NO_ID} "
            f"missing from {[(p['id'], p['token']) for p in top]}) -- raise N_PROBS")
    ly, ln = d[YES_ID], d[NO_ID]
    m = max(ly, ln)
    ey, en = pow(2.718281828459045, ly - m), pow(2.718281828459045, ln - m)
    return ey / (ey + en), en / (ey + en)


def _row_result(row: dict, p_yes: float, p_no: float, dt: float, n_reasoning_tokens: int) -> dict:
    passed = p_yes > p_no
    confidence = max(p_yes, p_no)
    return {
        "id": row["_id"], "truth": row["truth"], "passed": passed,
        "confidence": confidence, "p_yes": p_yes, "p_no": p_no,
        "latency_s": dt, "n_reasoning_tokens": n_reasoning_tokens,
    }


def judge_none(server: LlamaServer, renderer, row: dict) -> dict:
    t0 = time.time()
    prompt = render_prompt(renderer, build_question(row), enable_thinking=False)
    resp = server.complete(prompt, n_predict=1, n_probs=N_PROBS)
    p_yes, p_no = p_yes_no_from_probs(resp)
    return _row_result(row, p_yes, p_no, time.time() - t0, 0)


def judge_bounded(server: LlamaServer, renderer, row: dict, budget: int) -> dict:
    t0 = time.time()
    base_prompt = render_prompt(renderer, build_question(row), enable_thinking=True)
    reasoning_resp = server.complete(base_prompt, n_predict=budget, stop=["</think>"])
    reasoning_text = reasoning_resp.get("content", "")
    # Whether llama.cpp's `stop` matched naturally or the budget ran out,
    # `reasoning_text` never includes the stop string itself (llama.cpp
    # excludes it) -- so the closing separator always needs appending, same
    # as the MLX arms' forced-close path.
    full_prompt = base_prompt + reasoning_text + THINK_CLOSE_SUFFIX
    resp = server.complete(full_prompt, n_predict=1, n_probs=N_PROBS)
    p_yes, p_no = p_yes_no_from_probs(resp)
    n_tokens = reasoning_resp.get("tokens_predicted", len(reasoning_text.split()))
    return _row_result(row, p_yes, p_no, time.time() - t0, n_tokens)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=["none", "bounded"], required=True)
    ap.add_argument("--budget", type=int, default=200)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--base-url", default="http://127.0.0.1:8090")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sanity", action="store_true")
    args = ap.parse_args()

    server = LlamaServer(args.base_url)
    renderer = make_renderer()
    rows = load_hotel_400()
    if args.limit:
        rows = rows[: args.limit]
    print(f"scoring {len(rows)} rows, variant={args.variant} against {args.base_url}", flush=True)

    results = []
    aborted = False
    t0 = time.time()
    for i, row in enumerate(rows):
        pct = free_mem_pct()
        if pct is not None and pct < MIN_FREE_PCT:
            print(f"[ABORT] free memory {pct}% < {MIN_FREE_PCT}% floor at row {i}/{len(rows)}",
                  flush=True)
            aborted = True
            break
        if args.variant == "none":
            r = judge_none(server, renderer, row)
        else:
            r = judge_bounded(server, renderer, row, args.budget)
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
        "model": "lmstudio-community/Qwen3.6-35B-A3B-GGUF (llama-server)",
        "variant": args.variant, "budget": args.budget if args.variant == "bounded" else None,
        "n": len(results), "aborted_low_memory": aborted, "decisions": results,
    }, indent=2))
    print(f"wrote {args.out}")
    if aborted:
        sys.exit(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
