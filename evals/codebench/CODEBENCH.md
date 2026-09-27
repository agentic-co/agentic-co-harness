# codebench: does a gated repro→fix→validate procedure beat a bare prompt on real bugs?

A coding bug-fix benchmark built to find the optimal **local LLM setup** (Mac
Studio M3 Ultra 96GB, LM Studio on `:4242`, MLX models) for agentic coding —
and, along the way, to run the ASOP programme's central open question
(enforcement vs elicitation, see `ai-tasks/asop-eval/CONTEXT.md`) on a domain
neither prior arm has touched: **coding**, not customer-service SOPs.

Smoke-tested against GLM-4.7 on z.ai (`https://api.z.ai/api/coding/paas/v4`)
per the run brief — LM Studio was mid-run and off limits for this build. The
harness runs unchanged against any OpenAI-compatible `base_url` + `model` with
tool calling; see [Running the full sweep](#running-the-full-sweep).

## What this measures

Four arms, same model, same tools, same caps, on the same tasks:

- **bare** — "Fix the bug in `solution.py`." No scaffold.
- **pva** — a generic Plan→Act→Verify loop in prose (mirrors SOPBench's PVA
  idea). Not enforced.
- **asop** — an explicit 4-step procedure: REPRODUCE (write a test that
  demonstrates the bug; gated — it must FAIL against the current code),
  PROPOSE (write the fix), VALIDATE (the reproduction test AND the visible
  test must PASS; gated), then FINISH. Every gate is **code-checked**, not
  judge-checked: a subprocess exit code, not a model's opinion of one.
- **asop-nogate** — the *identical* procedure text as `asop`, but `finish()`
  is never refused. This is the arm that isolates the programme's central
  question: does the detailed procedure help via elicitation alone, or does
  it need enforcement to matter?

`bare` vs `asop` is the headline comparison. `asop` vs `asop-nogate` is the
mechanism question. `pva` is a second, more generic scaffold baseline, so a
result isn't "asop's exact wording" doing the work.

## Design decisions

- **Gate is code-checked, not judge-checked.** REPRODUCE and VALIDATE both
  gate on a subprocess exit code from running an actual test file. No LLM
  judge is in this loop anywhere — the whole point of testing on code is that
  "did it pass" has a ground truth that isn't a model's opinion.
- **The procedure is shown in full, not revealed one step at a time.** The
  τ²/SOPBench ASOP engine (`scripts/eval/asop_engine.py`) walks a procedure
  step by step because a customer-service SOP is a long multi-turn
  conversation where showing the whole thing invites "mentioned it, didn't do
  it." A single-file bug fix is a much shorter horizon; this build's time
  budget went to getting the gate itself right rather than porting stepwise
  reveal. Documented as a scope simplification, not silently different from
  the SOP-domain ASOP engine.
- **Every tool's exact arguments are stated in the procedure text**
  (`arms.py::system_prompt`) — the run brief called this out by name as the
  biggest failure mode found in N33/N35 (missing argument details). The
  REPRODUCE step spells out `write_file(path="repro_test.py", content=...)`
  and `run_tests(path="repro_test.py")` verbatim.
- **Hidden tests are never written to the agent's sandbox directory.** They
  exist only as a string on the `Sandbox` object and are written into an
  *isolated* temp copy at grading time, after the episode ends. There is no
  tool call that can reach them — not a permissions check, a file that never
  existed in the agent's reachable filesystem.
- **`asop` and `asop-nogate` get byte-identical system prompts.** The gate
  class (`AsopGate`) tracks the exact same state in both; `enforced=False`
  just makes `can_finish()` always return `True`. This is what makes the pair
  a clean enforcement-vs-elicitation isolation rather than two different
  prompts.
- **Reproduction-test validity is measured, not assumed.** An agent can write
  `repro_test.py` and have it pass by accident (e.g. it doesn't actually
  target the bug). The harness re-runs the agent's own repro test against the
  ORIGINAL buggy code (must fail) and the FINAL code (must pass), in isolated
  copies — both required for `repro_validity = True`.
- **"No network in the sandbox" is best-effort, not enforced at the OS
  level.** Subprocesses run with proxy env vars pointed at a closed local
  port (`127.0.0.1:9`) and a minimal, secret-stripped environment. `sandbox-exec`
  (macOS's syscall sandboxer) was tried and refused to exec even `/bin/echo`
  under SIP on this machine (verified 2026-09-25) — not viable here without
  more work than this build's budget allowed. A raw socket call from
  model-generated code is NOT blocked. Flagged, not silently claimed secure;
  a real fix is a Linux network namespace or a container (OrbStack exists,
  but its images are x86 — see the SWE-bench feasibility note below).
- **Caps**: 8 model turns, 20 tool calls, 3 gate refusals before a forced
  stop, 10s per tool call, 240s per episode wall clock. Chosen to be generous
  for a single-function fix without letting one stuck episode eat the run.
- **Concurrency**: `ThreadPoolExecutor`, default 2 (the z.ai run-brief cap;
  the principal's separate cap is 2 concurrent z.ai processes across ALL
  agents, coordinated, not per-run). Against LM Studio with one model loaded
  and `--parallel 1`, the server serializes every request regardless of how
  many this runner has in flight — `--concurrency 1` there, since higher
  buys no throughput and only risks a request timing out while queued behind
  another (`--request-timeout-s` exists to widen that margin if needed).
- **Sampling for a multi-model sweep**: `--sample N --seed S` picks a
  deterministic stratified sample (proportional across datasets) — the same
  `N` and `S` select the same `task_id`s on every model, which is what makes
  4 models' numbers comparable without running all ~195 tasks x 4 arms on
  each. `--resume` skips (task_id, arm) pairs already in the output file, for
  a run that got interrupted.

## Datasets

| Dataset | Tasks | Source | Licence | Revision |
|---|---|---|---|---|
| HumanEvalFix (Python) | 164 | `bigcode/humanevalpack` (HF), `python` config, `test` split | MIT | `9a41762f73a8` |
| QuixBugs (Python, flat subset) | 31 of 40 | `jkoppel/QuixBugs` (GitHub) | MIT | `4257f44b0ff1` |

Full provenance (fetch date, field-level notes, what got skipped and why) is
in `evals/codebench/data/<dataset>/PROVENANCE.md`, written by
`fetch_datasets.py` itself so it can't drift from what was actually fetched.

**Scope decision — QuixBugs graph-based programs excluded.** QuixBugs ships
40 buggy Python programs; 9 are graph algorithms (`breadth_first_search`,
`depth_first_search`, `detect_cycle`, `minimum_spanning_tree`,
`reverse_linked_list`, `shortest_path_length`, `shortest_path_lengths`,
`shortest_paths`, `topological_ordering`) graded by hand-written test
functions over a shared `Node` helper, not parametrized `[input, expected]`
pairs. This build's visible/hidden split is a slice over a case list — clean
for the other 31, wrong shape for the 9. Excluded and named here, not
silently dropped; a follow-up would give the graph subset its own splitter.

**Visible/hidden test split:**
- HumanEvalFix ships BOTH `example_test` (1-2 assertions, shown) and `test`
  (the full 5-8+ assertion held-out suite) as separate dataset fields — the
  split is authored upstream, not ours.
- QuixBugs ships one undifferentiated case list per program
  (`json_testcases/<name>.json`). This build's split takes the first
  `round(0.3 * n)` cases (minimum 1, always leaving at least 1 hidden) as
  visible; the rest are hidden. This split IS ours — documented in each
  program's contribution to `PROVENANCE.md`.

**SWE-bench Verified feasibility (assessed, not built).** OrbStack is
installed and Docker works, but the official SWE-bench Verified images are
x86-only — this machine is Apple Silicon, so every container would run under
Rosetta/QEMU emulation for the full run. That's a real cost (probably 2-4x
wall time per container, unmeasured here) on top of needing per-repo
environment images (dozens of GB) rather than the "plain Python, no Docker"
posture this build's brief called for. Verdict: feasible but expensive and
out of scope for a harness meant to compare *local model* setups quickly —
worth revisiting once arm64 images exist or if cross-arch cost is
acceptable. Not attempted.

## Metrics

- **pass@1** — hidden test suite exit code on the final `solution.py`, one
  sample per task (temperature 0.2, not 0 — z.ai's coding endpoint is a
  reasoning model and 0.2 is this build's default; full-run temperature is a
  `--temperature` flag).
- **Reproduction-test validity** (`asop`/`asop-nogate` only) — the agent's own
  `repro_test.py`, run against the original buggy code (must FAIL) and the
  final code (must PASS). `None` if the agent never wrote one.
- **Turns, tool calls, tokens (prompt/completion), wall time** — per episode,
  from the provider's own `usage` field where the endpoint returns one.
- **False "done"** — the agent called `finish()` at some point, but the
  hidden suite fails on the code it left behind. Tracked regardless of
  whether `finish()` was ever actually accepted (a `gate_exhausted` episode
  still counts if the agent tried to declare victory and got refused three
  times running).
- **Paired comparison** — exact McNemar on hidden pass/fail, paired by
  `task_id`, between any two arms (`metrics.paired_compare`). No scipy: exact
  McNemar is a closed-form sum over `math.comb`.

## Hypotheses (pre-registered before the full run)

- **H-C1**: `asop` pass@1 > `bare` pass@1.
- **H-C2**: `asop` pass@1 > `asop-nogate` pass@1 (enforcement matters when the
  gate has a real test oracle behind it — the central ASOP-programme
  question, tested here on a domain where "the gate fired" has never before
  been ambiguous the way it was on retail/airline SOPs).
- **H-C3**: `asop` false-done rate < `bare` false-done rate.

These are read against `metrics.paired_compare`'s exact McNemar p-value on
the full run's task set, not the 10-task smoke below (underpowered by
design — a smoke run's job is "does the harness work," not "is the effect
real").

## Smoke run

10 HumanEvalFix tasks × 4 arms = 40 episodes, GLM-4.7 on z.ai, concurrency 2,
default caps.

```
arm              n   pass@1  false_done  finished   turns tool_calls  prompt_tok  compl_tok   wall_s
----------------------------------------------------------------------------------------------------
bare            10    1.000       0.000     1.000     4.8        6.0        4476        461     27.2
pva             10    1.000       0.000     1.000     4.6        6.0        4902        578     31.7
asop            10    1.000       0.000     0.900     6.4        8.3        8704        836     44.8
asop-nogate     10    1.000       0.000     0.900     6.3        8.4        8611        840     39.5
```

All 10/10 pass@1 in every arm — the first 10 HumanEvalFix tasks plus GLM-4.7
give this smoke a ceiling effect (n=10 also has no discriminating power for
McNemar; H-C1/H-C2/H-C3 are read against the full run, not this table — see
[Hypotheses](#hypotheses-pre-registered-before-the-full-run)). What the smoke
DOES show: `asop`/`asop-nogate` wrote a `repro_test.py` on 10/10 tasks and
every one of those 10 was reproduction-VALID (failed on the original buggy
code, passed on the final fix) — the gate machinery is exercising real
code-checked state, not passing through inert. 1/10 `asop` and 1/10
`asop-nogate` episodes hit `cutoff_turns` (used all 8 turns without calling
`finish()`) and were graded on whatever `solution.py` held at cutoff — still
passed. `asop` costs roughly 1.6-1.9x `bare`'s tokens and turns for the same
smoke-level pass@1, which is the expected shape of a REPRODUCE+VALIDATE
procedure on tasks easy enough that `bare` already gets them all right; the
interesting comparison is on the harder tail, in the full run.

Commit: see `git log -1 -- evals/codebench scripts/eval/codebench` (local,
unpushed per repo convention — this is a public repo, nothing here is pushed)
· `uv run pytest -q`: **1765 passed, 3 skipped** (measured 2026-09-26,
includes this build's 27 `tests/test_codebench_*.py` cases — this repo's own
convention is "measure it, do not quote it"; treat this figure as already
possibly stale by the time it's read) · median wall time per episode on
GLM-4.7: **31.7s** (25.6s `bare` → 41.7s `asop`, medians per arm; raw
episodes in `evals/codebench/results/smoke-glm-4.7/episodes.jsonl`).

## Running the full sweep

Against z.ai (the smoke-tested target, default endpoint, needs `ZAI_API_KEY`
in the environment — never printed, never logged):

```
uv run python3 scripts/eval/codebench/run_codebench.py \
    --dataset all --arms bare,pva,asop,asop-nogate \
    --out evals/codebench/results/full-glm-4.7
```

Against LM Studio (one model loaded at a time, `--parallel 1` — hence
`--concurrency 1`), a 60-task stratified sample so 4 models x 4 arms stays
tractable (same 60 task_ids for every model, given the same `--sample --seed`):

```
uv run python3 scripts/eval/codebench/run_codebench.py \
    --dataset all --sample 60 --seed 20260926 \
    --arms bare,pva,asop,asop-nogate \
    --base-url http://localhost:4242/v1 --model <model-id> \
    --api-key-env NONE --concurrency 1 \
    --out evals/codebench/results/<model-id>
```

Resume an interrupted run (same flags, plus `--resume`):

```
uv run python3 scripts/eval/codebench/run_codebench.py \
    --dataset all --sample 60 --seed 20260926 --resume \
    --arms bare,pva,asop,asop-nogate \
    --base-url http://localhost:4242/v1 --model <model-id> \
    --api-key-env NONE --concurrency 1 \
    --out evals/codebench/results/<model-id>
```

Dataset re-fetch (only needed on a fresh clone — `evals/codebench/data/` is
committed):

```
uv run --with pyarrow python3 scripts/eval/codebench/fetch_datasets.py --source humanevalfix
python3 scripts/eval/codebench/fetch_datasets.py --source quixbugs
```

## Ceiling rule (2026-09-26 03:50 — written before the check)

Early qwen3-coder-next episodes: 18/18 pass across all four arms. Rule: at 60 episodes (15 tasks × 4 arms), if
`bare` pass@1 ≥ 0.95 the 60-task sample cannot separate the arms; stop the sweep and switch to a harder set
(all 31 QuixBugs + the HumanEvalFix tasks bare fails on, or SWE-bench-style repo bugs) before spending the other
three models' hours on it. Otherwise the sweep continues unchanged.

### Ceiling rule — FIRED 2026-09-26 ~04:00

qwen3-coder-next, first 60 episodes of the 60-task stratified sample (seed 20260926): bare 15/15, pva 15/15,
asop 15/15, asop-nogate 14/15. Sweep stopped at 62 episodes (kept). What it does say: a local 80B coder solves the
standard HumanEvalFix/QuixBugs format (bug + visible tests) essentially perfectly under every arm, so on this format
the procedure has nothing to add. Next: a ticket-only mode (no visible tests; the agent must reproduce from the
description) on a harder pool — the principal's actual use case (reproduce from a ticket with no external input).

## Ticket-only mode + harder pool (pre-registered 2026-09-26, before any run)

Two changes to the harness, both live in code (`--mode ticket-only`, `--difficulty harder`), the OLD default mode
and OLD 195-task pool are unchanged (the 62 qwen3-coder-next episodes above stay comparable to whatever runs
against `--difficulty all --mode default` later).

### `--mode ticket-only`

The agent gets the buggy code + the natural-language task description (HumanEvalFix's `instruction` field /
QuixBugs's docstring) and **no visible test at all** — the principal's real use case is reproducing a bug from a
ticket, not from a test file someone already wrote. Mechanism: `run_episode(..., ticket_only=True)` builds
`effective_task = dataclasses.replace(task, visible_test_code=None)` and passes that (not `task`) to the sandbox,
the prompt builders, and `AsopGate` — every one of those already keys off `visible_test_code` being falsy, so
hiding it there is the entire change. `task` itself, `hidden_test_code`, and `buggy_code` are untouched, so grading
is byte-identical to default mode. Verified in `tests/test_codebench_ticket_only.py` (visible_test.py never
written to the sandbox; `visible_test.py` mentions drop out of both the user prompt's files blurb and the asop/
asop-nogate system prompt's STEP 3; the gate's VALIDATE check no longer requires it).

This changes what REPRODUCE has to do. In default mode, a visible test plus the docstring both hint at intended
behavior; in ticket-only mode the agent's `repro_test.py` is the ONLY place that inference gets made — a shallow
or wrong reproduction is more likely, and would only be caught by the hidden suite at grading time, not by the
gate. That is a real prediction, not a wash: **H-C2t below is a genuinely harder bar for `asop`** than H-C2 was.

### Harder task pool (`--difficulty harder`)

All 31 QuixBugs tasks (no `bug_type` field — literature and the run brief already treat QuixBugs as the harder
set) **plus** the HumanEvalFix tasks whose `bug_type` is a single-token substitution rather than a whole line
added/removed. Split decided from the category names alone, before running anything or looking at any per-category
pass rate — see `datasets.HARDER_BUG_TYPES`/`EASIER_BUG_TYPES` for the exact criterion and reasoning:

| kept (harder) | excluded (easier) |
|---|---|
| value misuse (44), operator misuse (25), variable misuse (23), function misuse (8) | missing logic (33), excess logic (31) |
| **100 of 164** | 64 of 164 |

Harder pool = 31 QuixBugs + 100 HumanEvalFix = **131 of 195 tasks** (verified: `filter_harder(load_humanevalfix() +
load_quixbugs())` returns exactly 131, `tests/test_codebench_datasets.py`). `--sample 60 --seed 20260926` drawn
from that pool gives 46 HumanEvalFix / 14 QuixBugs (checked by hand — stratified_sample is proportional to
whatever pool it's handed, harder or not).

### Hypotheses, restated for ticket-only + harder

- **H-C1t**: `asop` pass@1 > `bare` pass@1, on the harder pool, `--mode ticket-only`.
- **H-C2t**: `asop` pass@1 > `asop-nogate` pass@1, same conditions — now the sharper test, since a wrong
  `repro_test.py` in ticket-only mode has no visible test to be caught against mid-episode.
- **H-C3t**: `asop` false-done rate < `bare` false-done rate, same conditions.
- **H-C4 (new, ticket-only only)**: `asop`/`asop-nogate` reproduction-validity rate is LOWER in `--mode
  ticket-only` than in `--mode default`, on the same task set — the mechanism prediction above, stated so it can
  be falsified rather than just asserted after the fact.

### Ceiling rule, generalized

Team-lead's rule that already fired once, restated for reuse on whatever sample runs next: **check `bare` pass@1
on the first 10 tasks x 4 arms (40 episodes) before committing to the full sample size.** If `bare` pass@1 ≥ 0.90
on that first 40, the sample is too easy for this comparison — stop and go harder (deeper into the harder pool,
`--mode ticket-only`, or both) before spending the remaining models' time on it.

### SWE-bench Verified/Lite feasibility on this machine — assessed, not built (per instruction)

OrbStack + Docker both work here (`docker version --format '{{.Server.Arch}}'` → `arm64` host), but every official
SWE-bench eval image checked (`docker manifest inspect` against a known `swe-bench.eval.x86_64.*` tag on
`ghcr.io`) returns a single-platform `linux/amd64` manifest — no arm64 variant exists, so every container runs
under OrbStack's x86 emulation, not natively. This is an ESTIMATE, not a measurement — no instance was actually run
(the instruction was explicitly not to build this):

- **Disk/download**: SWE-bench images are per-repo-version (an "environment" image, reused across instances of the
  same repo+commit) plus a smaller per-instance layer. Published sizes run from roughly 1-3GB to 10GB+ per
  environment depending on the repo's own dependency footprint (compiled deps, ML libraries, etc.); a 20-30
  instance subset spanning several repos (SWE-bench Verified draws from ~12 repos) would plausibly pull
  50-150GB+ on a cold cache. That alone is likely 1+ hour on a home connection.
- **Per-instance wall time**: native SWE-bench harness runs report single-digit to ~30 minutes per instance
  depending on the repo's test suite size (build + install + run tests). Under x86 emulation on Apple Silicon,
  informal community reports for compilation-heavy workloads put the slowdown at roughly 2-5x; a lighter
  interpreted-Python repo's tests would emulate closer to native. No number here is measured on THIS machine.
- **Verdict**: technically possible (Docker + emulation both work), but a 20-30 instance subset **tonight** is
  optimistic once the first cold-cache pull is counted — a more realistic same-session probe would be ~3-5
  instances from a single, lightweight repo (to bound the image-pull cost) purely to measure the real per-instance
  time on this machine, before committing to a larger subset in a later session with a pre-staged image cache.
  Not attempted, per instruction.

### Exact command — harder pool, ticket-only, LM Studio

Same 60-task stratified sample mechanism as before, now drawn from the 131-task harder pool (46 HumanEvalFix / 14
QuixBugs instead of 50/10) and run in ticket-only mode. LM Studio still one model at a time, `--parallel 1`, hence
`--concurrency 1`:

```
uv run python3 scripts/eval/codebench/run_codebench.py \
    --dataset all --difficulty harder --mode ticket-only \
    --sample 60 --seed 20260926 \
    --arms bare,pva,asop,asop-nogate \
    --base-url http://localhost:4242/v1 --model <model-id> \
    --api-key-env NONE --concurrency 1 --request-timeout-s 600 --resume \
    --out evals/codebench/results/local-<key>-harder-ticket
```

Task counts for that command: **60 episodes-worth of tasks x 4 arms = 240 episodes per model** (vs 60 x 4 = 240
in the original default/all sweep — same episode count, harder tasks). Drop `--sample 60` to run the FULL harder
pool (131 x 4 = 524 episodes/model — likely too long per model tonight given the ceiling rule already fired once
at 60; keeping `--sample 60` unless you want the larger run specifically). Keep `--out` distinct from the
`local-<key>` dirs already in flight — this proposal uses a `-harder-ticket` suffix so both modes' results sit
side by side under `evals/codebench/results/` rather than overwriting each other.

### Ticket-only ceiling rule — FIRED 2026-09-26 ~04:45

qwen3.6-35b-a3b, harder pool, ticket-only, first 40 episodes (10 tasks × 4 arms): bare 9/10, pva 9/10, asop 9/10,
asop-nogate 8/10 (bare ≥ 0.90 → rule fires). Failures are scattered — each arm fails a DIFFERENT task — and almost all
failures are runs that hit the 8-turn cap. Repro validity in the asop arms 17/20 True (H-C4's predicted drop is not
visible yet). Finding: a local 35B-A3B model fixes single-function bug tickets ~90% with or without a procedure, even
with no tests shown. Stopped at 41 episodes. Next: real multi-file repository bugs with symptom-only tickets.

## Repo tier: real multi-file bugs, generated by mutation (pre-registered 2026-09-26, before any run)

The next tier up, and the principal's actual use case: reproduce a bug from a ticket with no
test file, no visible tests, no pointer to which file or function is involved — across a WHOLE
repository, not one function. New harness code, same four arms, same harness conventions
(z.ai/LM-Studio-agnostic provider, JSONL episodes, `metrics.py` reused unmodified). Lives in
`scripts/eval/codebench/repobench/` (a subpackage — deliberately NOT edited into the existing
single-file tier's `agent.py`/`arms.py`/`sandbox.py`, which the LM Studio sweep was actively
reading from while this was built; see "Design decisions" below for what was forked vs reused).

### The 3 repos

| Repo | Commit | Licence | Baseline (this machine, 2026-09-26) |
|---|---|---|---|
| more-itertools | `fbb9a98d8c7b91` | MIT | 766 passed, 21202 subtests passed, 9.09s |
| toolz | `451af60dec590a` | BSD-3-Clause | 191 passed, 1 skipped, 1 deselected (metadata-only), 0.17s |
| boltons | `4e5faa3d7e4008` | BSD-3-Clause | 525 passed, 12 subtests passed, 3.56s |

All pure Python, root-level package layout (no `src/`, no install step — `sys.executable -m
pytest` from the repo root works with nothing but the interpreter this harness already runs
on). Full provenance (exact keep-paths, why each exclusion, the deselected test's reason) is in
`evals/codebench/data/repobench/<repo>/PROVENANCE.md`.

**The upstream source is not committed.** `evals/codebench/data/repobench/<repo>/repo/` is
gitignored: a vendored copy carries upstream authors' addresses and test fixtures that this
repo's leak scanner (correctly) refuses to publish, and a pin reproduces it exactly anyway. A
fresh clone must materialize the three snapshots once before any repobench run:

```
uv run python3 scripts/eval/codebench/repobench/snapshot.py
```

That clones each repo from GitHub, checks out the full commit SHA pinned in
`scripts/eval/codebench/repobench/repos.py` (git verifies the content against that hash, so a
tampered or moved upstream cannot silently substitute other code), and copies only the
`keep_paths` into `<repo>/repo/`. `mutants.jsonl` and each `PROVENANCE.md` stay committed —
they describe the pinned commit and are what `run_repobench.py` reads alongside the snapshot.

**Considered and dropped**: python-dateutil (its `src/` layout needs an install step, and its
zoneinfo tests need a local tzdata cache this build didn't set up — an environment gap, not a
code problem) and attrs (also `src/`, needs a wheel build backend). Neither was dropped for
anything about its source; flagged in `repos.py`'s docstring so a later build doesn't rediscover
this by hitting the same walls.

### Bug generation: AST mutation, kept only if the repo's OWN tests fail

`mutate.py` enumerates 5 mutation kinds — binary-operator swap, comparison-operator swap,
boolean-operator swap, constant/off-by-one swap (including `True`/`False`), and wrong-variable
swap (swaps every LOADED reference to the first two USED positional args across a function's
whole body) — via one deterministic `ast.walk`, keyed by `(lineno, col_offset, kind)` so a site
is stable across re-parses. `generate_tasks.py` shuffles candidate sites with a seeded
`random.Random(f"{seed}:{repo}")`, applies one at a time to a throwaway copy, runs the repo's OWN
pinned test command, and KEEPS the mutant only if it fails — an "equivalent mutant" (no behavior
change) is silently discarded, not counted. Deduped to at most one kept mutant per (file,
enclosing function), so the 60 spread across the codebase rather than clustering.

- **60 tasks generated** (seed `20260926`): 20 more-itertools, 20 toolz, 20 boltons.
- Hit rate was high — more-itertools needed 33 candidates tried to keep 20 (of 1404 sites
  found), toolz 22 tried of 467 sites, boltons 64 tried of 3137 sites.
- Mutation kinds actually kept: cmpop 21, constant 16, binop 13, varswap 10.
- A candidate that HANGS the suite (a mutated loop condition that never exits) is discarded, not
  kept with a synthetic "it timed out" ticket — deriving a clean ticket for a hang needs a
  different code path than the assert/exception tiers below, out of scope for this build.
- Full generation run: ~9 minutes (dominated by more-itertools' ~9s suite x tens of full runs).

### Tickets: mechanically derived, never the test or the function/file name

`ticket.py` takes the FIRST failing test's own `pytest --tb=long` output for ONE node id and
extracts a symptom sentence via 3 tiers, never touching the traceback's file/line text or the
test's source — only regex-matched VALUE groups, plus a redaction pass that removes the mutated
function's name and generic leak shapes (object addresses, `<function ... at 0x...>` reprs):

1. **assert-value** (25/60) — pytest's own `E   assert X == Y` line, or unittest's
   `E   AssertionError: X != Y` line → "produces `X`, but should equal `Y`" (or "should NOT
   equal", for a `!=`/`assertNotEqual`-shaped failure).
2. **exception** (31/60) — no clean assert diff (the mutation raised instead of returning wrong
   data) → the exception type + redacted message.
3. **fallback** (4/60) — neither matched (a multi-line diff too complex to parse cleanly) → a
   generic "something regressed, find it via the test suite" ticket. Tracked, not hidden — a
   real cost of not hand-writing 60 tickets.

**A polarity bug this build found and fixed before shipping**: unittest's `assertEqual`/
`assertNotEqual` failures use a FIXED message template (empirically verified — `assertEqual`
failing ALWAYS prints "A != B", `assertNotEqual` failing ALWAYS prints "A == B", regardless of
the source's own operator), which is the OPPOSITE polarity from pytest's own assertion-rewrite
text (which shows the SOURCE's literal operator). An earlier version of `ticket.py` used one
polarity mapping for both, which produced ticket text like "produces `[]`, but should NOT equal
`['a','b']`" — true but VACUOUS (the value already satisfies "not equal", so the ticket gives no
actionable signal). Caught by manual inspection of the first generated batch, fixed, and locked
in with `tests/test_repobench_ticket.py`'s 4 polarity-specific tests before any model ever saw a
ticket.

**3 example tickets** (verbatim, from the generated set):

> A behavior check in this project's own test suite is failing. The result currently produced is
> `[]`, but the check says it should equal `['a', 'b']`.
> — `more-itertools/002` (cmpop, `Is -> IsNot`, in a function whose name isn't shown here either)

> A behavior check in this project's own test suite is failing. Instead of completing normally,
> it raises `ValueError` with message: "Indices for islice() must be None or an integer: 0 <= x
> <= sys.maxsize.".
> — `toolz/001` (varswap)

> A behavior check in this project's own test suite is failing. Instead of completing normally,
> it raises `AttributeError` with message: "'set' object has no attribute '_included'".
> — `boltons/000` (varswap, `swap self/other` in a dunder method)

### Design decisions

- **Anti-tampering grading**: `RepoSandbox.run_hidden_test` restores every test-marked file to
  its PRISTINE snapshot content before running the full suite, in an isolated temp copy. Without
  this, an agent could "fix" a task by deleting or neutering the failing assertion instead of the
  bug — verified directly in `tests/test_repobench_sandbox.py`: the agent's OWN `run_tests()`
  view reflects a tampered test as green mid-episode, but `run_hidden_test` ignores the tamper
  and still fails.
- **`localisation`**: did the final code differ from what the agent STARTED with (the buggy
  file), not from the pre-bug original (which differs from the buggy start by construction,
  always — comparing against that would read `True` even if the agent touched nothing). Simple
  bug this build caught in its own first test run and fixed before it became a metric artifact.
- **`repro_test.py` must be a real pytest test** (`def test_...():`), not a bare top-level
  `assert` — this tier's tests run through pytest (matching the repo's own command), and a bare
  assert at module scope is a collection-time statement, not a collected test item. The prompt
  says this explicitly (`repo_arms.py`'s tools blurb) after `tests/test_repobench_sandbox.py`
  demonstrated the difference directly.
- **`RepoAsopGate` is a small deliberate FORK of the single-file tier's `AsopGate`**, not a reuse
  — VALIDATE here has two conditions (the repro test passes AND the WHOLE suite passes,
  `run_tests()` with no path), and the existing class's visible-test check is keyed to a literal
  `path == "visible_test.py"` string that a `None` path can't satisfy without bending its
  meaning. ~40 lines forked, tested the same way (`tests/test_repobench_arms.py`).
- **Mutated source is recomputed at run time, not stored per task** — `mutants.jsonl` records
  `(mutated_file, mutation_kind, mutation_detail, mutation_lineno)`; `run_repobench.py` reruns
  `find_sites` + `apply_mutation` against the still-pristine snapshot to reproduce the exact
  mutant. Cheaper than storing 60 full files, at the cost that a future change to `mutate.py`'s
  site-finding could silently stop matching an old task's recorded site — acceptable for a build
  whose mutation logic is itself locked down by `tests/test_repobench_mutate.py`.
- **Caps raised per the run brief's finding** (the single-file tier's 8-turn cap was the binding
  constraint on the ticket-only ceiling run): 20 turns, 40 tool calls, 4 refusals, 600s episode
  timeout, 20s per tool call (more-itertools' own suite alone needs ~9s).
- **Structural, no-network verification only** — this build did NOT run against z.ai (told not to
  spend further z.ai calls) or LM Studio (in use elsewhere). Instead: `tests/test_repobench_agent.py`
  runs the FULL episode loop (`run_repo_episode`) end to end against a scripted FAKE model
  (`monkeypatch.setattr(repo_agent, "chat", ...)`) over a small real two-module toy repo —
  exercising tool dispatch, the gate, hidden grading, localisation, and regression counting
  together, including a scenario where the agent introduces then reverts its own regression, and
  one where a regression survives to the end and `regression_count` reports it. This is NOT a
  substitute for a real model run (no LLM reasoning is exercised, no ceiling-rule evidence exists
  yet for this tier) — it is evidence the HARNESS'S OWN plumbing is correct before spending any
  model budget on it.

### Hypotheses (pre-registered before any model run on this tier)

- **H-R1**: `asop` pass@1 > `bare` pass@1.
- **H-R2**: `asop` pass@1 > `asop-nogate` pass@1 (enforcement matters — now on real multi-file
  localisation + fix + no-regression, not a single function).
- **H-R3**: `asop` false-done rate < `bare` false-done rate.
- **H-R4 (new for this tier)**: `asop`'s `localisation` rate (did the agent even find the right
  file) is higher than `bare`'s — a repro-test-first procedure gives an earlier, cheaper signal
  ("my test still can't reproduce this") than only discovering the wrong file at hidden-grading
  time.
- **H-R5 (new for this tier)**: `asop`'s mean `regression_count` is lower than `bare`'s — the
  VALIDATE step's whole-suite check is specifically designed to catch a regression before
  `finish()`, which `bare` has no mechanism to do.

### Ceiling rule (same rule, restated for this tier)

Check `bare` pass@1 on the first 10 tasks x 4 arms (40 episodes) before committing to the full
60. If `bare` pass@1 ≥ 0.85 on that first 40 (looser bar than the single-file tier's 0.90-0.95,
since real multi-file localisation is harder by construction — a lower ceiling threshold here
would flag a still-too-easy sample sooner), the sample is too easy for this comparison: stop and
reconsider (a 4th/5th repo, a stricter mutation selection, or accept that this model tier has
plateaued on symptom-only single-repo bugs too).

### Exact command

Smoke (2 bugs x 4 arms = 8 episodes — run this FIRST, size small enough to sanity-check before
committing a model's time to the full 60):

```
uv run python3 scripts/eval/codebench/repobench/run_repobench.py \
    --limit 2 --out evals/codebench/results/repobench-smoke-<key>
```

Full 60-task run against LM Studio (one model loaded, `--parallel 1`, hence `--concurrency 1`,
same reasoning as the single-file tier; `--resume` for an interrupted run):

```
uv run python3 scripts/eval/codebench/repobench/run_repobench.py \
    --base-url http://localhost:4242/v1 --model <model-id> \
    --api-key-env NONE --concurrency 1 --request-timeout-s 600 --resume \
    --out evals/codebench/results/repobench-local-<key>
```

On a fresh clone, run `snapshot.py` (below) first — the snapshots are gitignored, see Repos.

Regenerating the mutant set (only needed if `mutate.py`/`generate_tasks.py` change, or to grow
past 60 — deterministic given the same `--seed`, so a bare re-run reproduces the same 60):

```
uv run python3 scripts/eval/codebench/repobench/snapshot.py       # re-fetch the 3 pinned repos
uv run python3 scripts/eval/codebench/repobench/generate_tasks.py --per-repo 20 --seed 20260926
```

### Repo-tier smoke, GLM-4.7, 2026-09-26 ~07:00 — INVALID as a comparison (harness defects)
Raw: bare 5/6, pva 2/6, asop-nogate 3/6, asop 0/6 (asop 6/6 hit the 20-turn cap, 0 valid repros). Trajectory
inspection (asop more-itertools/000): correct localisation + a valid failing repro, then a whole-file `write_file`
of a multi-thousand-line module (likely truncated) and the rest of the episode spent trying to recover it —
including pip/urllib attempts to reach PyPI from the sandbox. Defects: (1) no edit/patch tool, only whole-file
replacement; (2) network isolation unverified. Not evidence about ASOP. Harness fix + identical re-smoke queued
(agent codebench), pre-registered as a harness change, not an arm change.

### Harness fix (pre-registered 2026-09-26, before the re-smoke) — this is an INFRASTRUCTURE
### change, not an arm change; H-R1..H-R5 and the ceiling rule are unaffected

**Root cause, confirmed**: `provider.chat`'s `max_tokens` was hardcoded to 2048 and never
overridden by either tier's agent loop. `more_itertools/more.py` is 5635 lines; a single
`write_file` rewriting the whole thing needs far more than 2048 completion tokens. GLM's
structured tool-calling still closed the JSON cleanly at the cutoff (verified: the `write_file`
call in the smoke's `more-itertools/000` asop trajectory reports `ok=True` — a parse failure
would have shown up as an "unknown tool"/args-parse error instead), so the truncation was
INVISIBLE to the harness — it just silently shortened the file. This is not a guess: `finish_reason`
from the API response was never even captured before this fix (confirmed by reading `provider.py`
— the field was discarded from `choices[0]`), so there was no way to have noticed.

**Fixes, in the order of the run brief's numbered list**:

1. **`edit_file(path, old, new)`** — exact-match string replacement, unique-match required (a
   non-unique or absent `old` refuses with a specific error). `write_file` now REFUSES on an
   existing file over 200 lines, pointing the caller at `edit_file` in the refusal text itself.
   All 4 arms' prompts (`repo_arms.py`) now say to use `edit_file` for existing-file changes,
   and the ASOP procedure's STEP 2 states edit_file's exact args inline (`edit_file(path=<the
   source file>, old=<the exact buggy lines, copied from read_file's output>, new=<your fix>)`)
   per the N33 lesson the run brief named. `RepoAsopGate` treats `edit_file` and `write_file` as
   equivalent PROPOSE actions — the gate doesn't care which tool changed a file, only that one
   did. 11 new tests (`edit_file` unique/absent/duplicate/nonexistent-file cases, `write_file`'s
   size refusal and its still-allowed short-file case, the gate recognizing `edit_file`, and a
   full scripted-model episode using `edit_file` for STEP 2 end to end).
2. **`max_tokens`**: `provider.DEFAULT_MAX_TOKENS` raised 2048 -> 8192; `finish_reason` is now
   captured on every `ChatResult` and threaded into `RepoEpisodeResult.any_truncated` (true if
   ANY turn's response ended with `finish_reason == "length"`). `max_tokens` itself is now logged
   per episode (`RepoEpisodeResult.max_tokens`) and exposed as `run_repobench.py --max-tokens`. The
   OLD smoke's episodes predate this field, so exact truncation cannot be confirmed for THEM
   directly — but scanning their `tool_call_log` for a `write_file` targeting a file over 200
   lines (the same threshold `write_file` now refuses on) found: pva 5/6, asop 3/6, asop-nogate
   3/6, bare 1/6 episodes attempted at least one risky whole-file rewrite of a large module. That
   is a proxy (attempted risky writes), not a confirmed-truncated count — the new re-smoke reports
   the real thing via `any_truncated`.
3. **Network isolation — the proxy-env approach had a real gap, now closed.** Verified directly:
   `urllib.request` to a real host WAS correctly blocked by the dead-proxy env vars (connection
   refused in ~6ms — this mechanism worked as designed for HTTP-client libraries that check
   `http_proxy`/`https_proxy`). A raw `socket.connect()` was NOT blocked by it — confirmed by
   opening one directly against `8.8.8.8:443` from inside the old sandbox and watching it succeed <!-- leakguard: allow — public DNS host used as the reachable-target probe, not an internal address -->
   at the TCP level. Re the smoke's own pip/urllib attempts specifically: `pip` isn't installed as
   a module in this venv (`No module named pip`) and the `uv` binary here has no `download`
   subcommand, so those specific attempts failed for unrelated reasons, not because isolation
   held — but the urllib attempt WAS correctly blocked. Net: no actual exfiltration occurred in
   the observed transcript, but the raw-socket gap was real. **Closed** with a `sandbox-exec`
   profile (`(allow default)(deny network-outbound)(deny network-bind)` — `(deny default)` was
   tried first and refused to exec even `/bin/echo` under SIP on this machine, consistent with
   what the single-file tier's `sandbox.py` already found; `(allow default)` plus explicit denies
   is what actually works). Verified: the SAME raw-socket connect now gets `PermissionError`
   (kernel-level denial, not a proxy miss), and a full pytest run through the wrapper reports the
   IDENTICAL pass count as unwrapped (toolz: 191 passed, 1 skipped, 1 deselected — no regression
   in normal use). `RepoSandbox.network_isolation_mode()` reports which mode is active
   (`"sandbox-exec"` here; falls back to `"proxy-env-only"` with a reported degradation on a
   non-macOS host or if `sandbox-exec` is missing). This closes the gap for the repo tier
   specifically (scope of this smoke) — the single-file tier's `sandbox.py` has the identical
   proxy-only gap and was not touched here (out of scope for this fix, flagged for whoever picks
   it up next).
4. **Damaged-file counting**: new `RepoSandbox.damaged_files()` / `RepoEpisodeResult.damaged_files`
   — any non-test source file that shrank more than 20% versus what the agent STARTED with (the
   buggy content for the mutated file, the pristine snapshot for every other file). Cannot be
   computed for the OLD smoke (final file content was never stored, only `tool_call_log` with
   `content`/`old`/`new` stripped to keep records small) — see point 2's proxy count instead. The
   NEW re-smoke reports this exactly, per episode and aggregated per arm.

None of this touches `arms.py`/`agent.py`/`sandbox.py` (the single-file tier, still mid-sweep-safe)
except `provider.py`, which is genuinely shared and where the `max_tokens`/`finish_reason` fix
lives — that change is purely additive (a higher default, a new optional field) and cannot change
any already-recorded single-file-tier episode's behavior retroactively.

**Re-smoke command** (same 6 bugs x 4 arms = 24 episodes, z.ai, 1 stream):

```
uv run python3 scripts/eval/codebench/repobench/run_repobench.py \
    --limit 2 --out evals/codebench/results/repobench-smoke-glm-4.7-v2
```
