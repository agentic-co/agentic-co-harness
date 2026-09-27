#!/usr/bin/env python3
"""Validate a llama.cpp/GGUF decider route against the official torch weights.

    python3 scripts/eval/decider_gguf_route_validate.py \\
        --torch-url http://127.0.0.1:4251/v1/systemone \\
        --llama-url http://127.0.0.1:4260/completion \\
        --choice-temp 1.164 --n 20

WHY THIS EXISTS
---------------
`decider replay v3` (QUEUED-EXPERIMENTS.md) needed to test `decider-35b-a3b` -- 65 GB bf16,
too big to co-load -- via a community GGUF conversion instead. Before spending 21-37 GB and
real wall-clock on a big-model run, the pre-registered plan gates on THIS script: does a
GGUF served by llama-server, read the way this script reads it, agree with the official
torch weights on a small, cheap model (decider-2b) it CAN run side-by-side? If not, the
GGUF path is not a faithful stand-in and nothing downstream of it means what it appears to.

THE READOUT
-----------
Reproduces decider's plain-layout, state-first prompt text exactly (decider/prompt.py
`build()`, single question, narrow <=10 options):

    Context:
    <state>

    Question: <question text>
    Options:
    (A) <opt0>
    (B) <opt1>
    Answer: (

...sent to llama-server's `/completion` with `n_probs`, reading the raw (temperature-1)
logprobs at the option-letter tokens ("A"/"B"/...). `llama-server`'s `n_probs` returns those
raw logprobs regardless of the request's own `"temperature"` field -- verified empirically
(identical `top_logprobs` at temperature 1.0/2.0/0.5 on the same prompt), so decider's own
per-type temperature is applied by hand: `p_i = exp(logprob_i / T)`, renormalized over just
the option letters actually used. This is EXACT, not an approximation: the shared full-vocab
log-normalizer in `logprob_i = logit_i - log(sum_full exp(logit))` is common to every option
and cancels in the ratio regardless of `T`, so restrict-then-renormalize equals softmax
computed directly on the option subset at that temperature -- precisely what decider's own
torch inference does (`decider.temperature`, `decider.model`).

RESULT, 2026-09-26 (recorded in QUEUED-EXPERIMENTS.md "decider replay v3")
---------------------------------------------------------------------------
FAILED on `decider-2b`, both Q8_0 and F16 GGUF (`cosetoenor/decider-2b-GGUF`) against torch:
15/20 (75.0%) agreement, mean |dp| ~0.12 -- against the pre-registered bar of >=95% / ~0.05.
Q8_0 and F16 disagreed with torch on the IDENTICAL 5 rows with near-identical |dp| (third
decimal), which rules out quantization noise. Leading hypothesis (not chased further, per the
"stop and report" rule the gate exists to trigger): `decider-2b`'s `decider_config.json`
declares a non-default RoPE scheme (`mrope_interleaved: true, mrope_section: [11, 11, 10],
partial_rotary_factor: 0.25`) that a community GGUF conversion of a brand-new architecture
(Qwen3.5) is a plausible place to get wrong. `decider-35b-a3b` was never downloaded to
completion or run as a consequence.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
from sopbench_judge import stratified  # noqa: E402

LETTERS = "ABCDEFGHIJ"

QUESTION = {
    "type": "choice",
    "instructions": (
        "An action has stated prerequisites. Given the evidence (tool history, the "
        "action and its prerequisites, and the conversation transcript), were the "
        "prerequisites actually satisfied?"
    ),
    "criteria": {
        "held": "the evidence establishes every prerequisite WAS satisfied",
        "not_held": "the evidence does not establish the prerequisites were satisfied, or shows one was not",
    },
}


def body_of(ev: dict) -> str:
    tool_history = "TOOL HISTORY:\n" + "\n".join(ev["tool_history"])
    action = f"ACTION AND PREREQUISITES: {ev['step_body']}"
    transcript = "TRANSCRIPT:\n" + "\n".join(ev["transcript"])
    return f"{tool_history}\n\n{action}\n\n{transcript}"


def torch_ask(url: str, state: str) -> dict:
    payload = {"state": state, "questions": {"verdict": QUESTION}}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60))["answers"]["verdict"]


def llama_ask(url: str, state: str, temperature: float, decider_repo: str) -> dict:
    """decider_repo: path to a checkout of github.com/Mapika/decider (for `decider.systemone`'s
    `render_state`/`render_question` -- the exact same rendering torch uses)."""
    sys.path.insert(0, decider_repo)
    from decider.systemone import render_question, render_state  # noqa: PLC0415

    rq = render_question(QUESTION)
    ctx = render_state(state)
    opts = rq["options"]
    assert len(opts) <= len(LETTERS), f"{len(opts)} options exceeds the narrow A-J rendering"
    prompt = ("Context:\n" + ctx + "\n\nQuestion: " + rq["question"] + "\nOptions:"
              + "".join(f"\n({LETTERS[j]}) {o}" for j, o in enumerate(opts))
              + "\nAnswer: (")
    payload = {"prompt": prompt, "n_predict": 1, "n_probs": 20, "temperature": 1.0,
               "top_k": 0, "top_p": 1.0, "min_p": 0.0, "cache_prompt": False}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    resp = json.load(urllib.request.urlopen(req, timeout=120))
    top = resp["completion_probabilities"][0]["top_logprobs"]
    letter_logprob = {}
    for j in range(len(opts)):
        letter = LETTERS[j]
        for t in top:
            if t["token"] == letter:
                letter_logprob[letter] = t["logprob"]
                break
    if len(letter_logprob) < len(opts):
        missing = [LETTERS[j] for j in range(len(opts)) if LETTERS[j] not in letter_logprob]
        return {"error": f"letters not in top_logprobs (raise --n-probs): {missing}"}
    scaled = {L: math.exp(lp / temperature) for L, lp in letter_logprob.items()}
    tot = sum(scaled.values())
    probs = {L: v / tot for L, v in scaled.items()}
    j_best = max(range(len(opts)), key=lambda j: probs[LETTERS[j]])
    return {"choice": rq["names"][j_best], "x_p_max": probs[LETTERS[j_best]],
            "probabilities": {rq["names"][j]: probs[LETTERS[j]] for j in range(len(opts))}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--decisions", type=Path, default=Path("/tmp/sb/hotel_native.jsonl"))
    ap.add_argument("--torch-url", required=True, help="the official torch decider.serve /v1/systemone")
    ap.add_argument("--llama-url", required=True, help="llama-server's /completion for the GGUF under test")
    ap.add_argument("--choice-temp", type=float, required=True,
                    help="decider_config.json temperature_by_type.choice for this model/version")
    ap.add_argument("--decider-repo", default=str(Path.home() / "Tools/decider-runtime/repo"))
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--per-class", type=int, default=200)
    ap.add_argument("--seed", type=int, default=20260923)
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.decisions) if l.strip()]
    sample = stratified(rows, args.per_class, args.seed)[:args.n]

    agree, total, prob_diffs = 0, 0, []
    for r in sample:
        state = body_of(r["evidence"])
        t = torch_ask(args.torch_url, state)
        g = llama_ask(args.llama_url, state, args.choice_temp, args.decider_repo)
        if "error" in g:
            print(f"{r['_id']}: LLAMA ERROR {g['error']}")
            total += 1
            continue
        total += 1
        match = t["choice"] == g["choice"]
        agree += match
        pd = abs(t["x_p_max"] - g["x_p_max"])
        prob_diffs.append(pd)
        print(f"{r['_id']:40s} truth={r['truth']:9s} torch={t['choice']:9s}({t['x_p_max']:.3f}) "
              f"llama={g['choice']:9s}({g['x_p_max']:.3f})  match={match}  |dp|={pd:.3f}")

    print()
    print(f"agreement: {agree}/{total} = {agree/total:.1%}  (bar: >=95%)")
    if prob_diffs:
        print(f"mean |dp|: {sum(prob_diffs)/len(prob_diffs):.4f}  max |dp|: {max(prob_diffs):.4f}  (bar: ~0.05)")
    ok = total > 0 and agree / total >= 0.95 and (not prob_diffs or sum(prob_diffs) / len(prob_diffs) <= 0.05)
    print("GATE:", "PASS -- proceed to the big model" if ok else "FAIL -- stop and report, per the pre-registered rule")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
