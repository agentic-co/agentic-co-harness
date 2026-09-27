#!/usr/bin/env python3
"""Generate one deterministic Python precondition-checker per SOPBench
constraint, using GLM-4.7 on z.ai -- idea 3 ("System 2 writes System 0") from
ai-tasks/local-s1s2/PLAN.md.

ANTI-LEAKAGE (enforced structurally, not just by convention): the ONLY inputs
this script ever puts in a generation prompt are
  (a) SOPBench's own constraint verbalizations + tool schemas, dumped once to
      evals/s0checks/<domain>/domain_schema.json by a one-off `python -c`
      against `env.domains.<domain>.<domain>_assistant` (see PLAN.md), and
  (b) synthetic tool-output/params examples from `precondition_specs.py`,
      invented by hand, never touching `default_data`, any task, or
      `jev.verdicts.jsonl`.
This script never opens `jev.verdicts.jsonl`, any `*.verdicts.jsonl`, any
`confirm/gate/jev/**/*.json` trajectory, or SOPBench's `env/dep_eval.py` /
`env/domains/*/[a-z]*.py` (state-tracker ground truth). It also never calls
`grade_judge_gate.truth_of`. Grading lives in `score_hotel.py`, a separate
script that runs after generation and after the pre-registration is written.

Usage:
    python3 generate_checks.py --domain hotel \
        --schema evals/s0checks/hotel/domain_schema.json \
        --outdir evals/s0checks/hotel
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_ZAI_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"
_ENV_FILE = Path.home() / ".claude" / ".env"
MODEL = "glm-4.7"


def _env_value(key: str) -> str | None:
    val = os.environ.get(key)
    if val:
        return val
    try:
        for line in _ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def zai_chat(user_message: str, retries: int = 5) -> tuple[str, dict]:
    """One z.ai chat completion. Returns (content, usage_dict). Never prints the key."""
    key = _env_value("ZAI_API_KEY")
    if not key:
        raise SystemExit("ZAI_API_KEY not found in env or ~/.claude/.env")
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": user_message}],
        "temperature": 0.2,
    }
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                _ZAI_URL, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
            resp = json.load(urllib.request.urlopen(req, timeout=120))
            content = resp["choices"][0]["message"]["content"]
            usage = resp.get("usage", {})
            return content, usage
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 529) and attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            raise
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            raise
    raise RuntimeError("unreachable")


PROMPT_TEMPLATE = """You are writing a deterministic Python precondition checker for an agent \
gate. The gate calls your function to decide, from evidence already collected by tool calls \
in a conversation, whether a stated precondition holds before an action is allowed to proceed.

DOMAIN: {domain_name}

TOOLS AVAILABLE TO THE ASSISTANT (name, description, JSON parameter schema):
{tools_json}

PRECONDITION TO IMPLEMENT: "{name}"

Positive description (fill in the {{braces}} with the actual values at hand -- this describes \
when the underlying condition is literally TRUE):
  {positive}

Negative description (for reference only -- describes when it is FALSE; DO NOT implement the \
negation, implement the condition as named, matching the POSITIVE description above):
  {negative}

Your function will be called as:

    def check(tool_results: dict, params: dict) -> bool | None:
        ...

where:
  - tool_results: for every tool the assistant has called so far in this conversation, \
tool_results[tool_name] is that tool's PARSED return value, in the shape implied by the tool's \
own schema/description above (a dict, list, bool, number, or string). If a tool has not been \
called yet, it is simply ABSENT from tool_results -- never assume it is there.
  - params: a flat dict of every value the description above references by name (e.g. \
"room_type", "check_in_date", "amount", "guest_name", and any of the numeric/threshold names \
that appear in {{braces}}, such as min_age, max_stays, modification_deadline_hours, \
valid_document_types, check_in_time, check_out_time, ...), already resolved to concrete values \
for this specific request.

Return:
  - True  if the evidence in tool_results (plus params) establishes the condition IS satisfied,
  - False if it establishes the condition is NOT satisfied,
  - None  if the evidence available is not sufficient to decide (e.g. a tool you need was never \
called, or a value you need is missing from params) -- ALWAYS prefer None over guessing.

Rules:
  - Pure function. Standard library only (datetime, re, math, etc.). No I/O, no network, no \
imports beyond the standard library.
  - Defensive: use tool_results.get(...) / params.get(...), never tool_results[...] directly, \
and return None (not an exception) when something needed is missing or malformed.
  - Do not hardcode any example's specific values below (room numbers, guest names, dates, \
prices, reason strings) -- your function must work for ANY request in this domain, not just \
the example. The examples below exist ONLY to show you the SHAPE of the data.
  - Implement exactly what the positive description says -- no more, no less.

SYNTHETIC EXAMPLES (invented to show shape only -- fictitious values, not real logged cases, \
not something to special-case):
  Example tool_results:
{tool_results_json}
  Example params:
{params_json}

Respond with ONLY a single fenced python code block containing the function (and any small \
helper functions/constants it needs). No prose before or after the code block.
"""


def build_prompt(domain_name: str, tools: list[dict], name: str, positive: str, negative: str,
                  hint_tools_map: dict, synthetic_tool_outputs: dict, synthetic_params: dict) -> str:
    hint_tools = hint_tools_map.get(name, [])[:2]
    tool_results_example = {t: synthetic_tool_outputs[t] for t in hint_tools if t in synthetic_tool_outputs}
    params_example = synthetic_params.get(name, {})
    # keep the tool catalog to name/description/parameters only (no "strict" plumbing)
    slim_tools = [{"name": t["name"], "description": t["description"], "parameters": t["parameters"]} for t in tools]
    return PROMPT_TEMPLATE.format(
        domain_name=domain_name,
        tools_json=json.dumps(slim_tools, indent=2),
        name=name,
        positive=positive,
        negative=negative,
        tool_results_json=json.dumps(tool_results_example, indent=2),
        params_json=json.dumps(params_example, indent=2),
    )


_CODE_BLOCK = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)


def extract_code(content: str) -> str:
    m = _CODE_BLOCK.search(content)
    if not m:
        raise ValueError("no fenced code block in model response")
    return m.group(1).strip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--domain", required=True)
    ap.add_argument("--domain-name", default=None, help="human label for the prompt, default = --domain")
    ap.add_argument("--schema", type=Path, required=True)
    ap.add_argument("--outdir", type=Path, required=True)
    ap.add_argument("--specs", default="precondition_specs",
                    help="module (in this dir) defining PRECONDITIONS/HINT_TOOLS/SYNTHETIC_*")
    ap.add_argument("--only", nargs="*", default=None, help="subset of precondition names")
    args = ap.parse_args()

    import importlib
    specs = importlib.import_module(args.specs)
    PRECONDITIONS, HINT_TOOLS = specs.PRECONDITIONS, specs.HINT_TOOLS
    SYNTHETIC_TOOL_OUTPUTS, SYNTHETIC_PARAMS = specs.SYNTHETIC_TOOL_OUTPUTS, specs.SYNTHETIC_PARAMS

    schema = json.loads(args.schema.read_text())
    tools = schema["actions"]
    pos = schema["positive_constraint_descriptions"]
    neg = schema["negative_constraint_descriptions"]

    names = args.only or PRECONDITIONS
    gen_dir = args.outdir / "generation"
    checks_dir = args.outdir / "checks"
    gen_dir.mkdir(parents=True, exist_ok=True)
    checks_dir.mkdir(parents=True, exist_ok=True)

    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for name in names:
        if name not in pos:
            print(f"SKIP {name}: not in domain_schema positive_constraint_descriptions")
            continue
        prompt = build_prompt(args.domain_name or args.domain, tools, name, pos[name], neg.get(name, "(n/a)"),
                               HINT_TOOLS, SYNTHETIC_TOOL_OUTPUTS, SYNTHETIC_PARAMS)
        print(f"generating {name} ...", flush=True)
        content, usage = zai_chat(prompt)
        for k in total_usage:
            total_usage[k] += usage.get(k, 0) or 0
        try:
            code = extract_code(content)
            error = None
            compile(code, f"<{name}>", "exec")
        except Exception as exc:
            code = None
            error = f"{type(exc).__name__}: {exc}"
        (gen_dir / f"{name}.json").write_text(json.dumps(
            {"model": MODEL, "precondition": name, "prompt": prompt, "response": content,
             "usage": usage, "extract_error": error}, indent=2))
        if code is not None:
            (checks_dir / f"{name}.py").write_text(code)
            print(f"  ok -> {checks_dir / (name + '.py')}")
        else:
            print(f"  FAILED to extract/compile: {error}")

    print("total usage:", total_usage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
