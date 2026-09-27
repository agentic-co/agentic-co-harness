# T2 procedure ladder — the remaining SOPBench domains

**Companion to `evals/sopbench-bank-asop/T2-PROCEDURE-LADDER.md`.** That page measured `bank`,
`online_market` and (in its addendum) `library`. This page carries the rest of the seven:
`hotel`, `healthcare`, `dmv`, `university`. Same executor, same four arms, same evaluator, same
`score_ladder.py` — **nothing new was invented here, and nothing was re-derived.** Read the parent
page first; its Method, Bounds and Provenance sections govern this one unchanged.

One combined file rather than four per-domain files, because the only thing these four domains add
beyond their own numbers is the **cross-domain pattern**, and that pattern cannot be stated in a
file that sees one domain.

## Status — **PAUSED 2026-09-24 07:5x, GPU yielded to the V2 iteration front**

| domain | arms run | scored | n paired | note |
|---|---|---|---|---|
| `hotel` | 4/4 | ✅ | 195 | full task set, zero exclusions |
| `healthcare` | **2/4 complete, 2 partial** | ❌ | — | **paused mid-run — see the resume point below** |
| `dmv` | 0/4 | — | — | never started; 97 task specs |
| `university` | 0/4 | — | — | never started; 42 task specs — **see the power caveat below** |

🛑 **This page is incomplete, and the incompleteness is a scheduling decision, not a result.**
`healthcare`, `dmv` and `university` were breadth on an already four-times-replicated finding; the
V2 front found a defect on the critical path and had priority for the shared GPU. **Do not read the
absent rows as null results.**

### Resume point — `healthcare`

Paused by stopping the driver and the two in-flight arms. Nothing needs re-running:
`run_simulation.py` calls `load_existing_results()` and skips tasks already present, and it
**saves after every task** (`run_simulation.py:479`, inside the task loop), so re-issuing an arm's
original command with the same `--output_dir` resumes from the last completed task.

| arm | tasks written | state |
|---|---|---|
| `none` | **124 / 124** | ✅ complete |
| `constraint_hint` | **124 / 124** | ✅ complete |
| `action_order` | 78 / 124 | ⏸ partial |
| `pva` | 67 / 124 | ⏸ partial |

All four files were verified to parse and contain zero empty interactions after the stop. The two
partial arms lost at most the single task each was mid-way through, which resumption re-runs.

**`healthcare` was deliberately NOT scored.** Tasks execute in a fixed order grouped by action
type, so the first 67 are a *structured* subset, not a random sample — scoring the intersection
would produce a number that looks like a result and is not one. It is scored when the two partial
arms finish, and not before.

⚠️ **Durability:** the raw trajectories (133 MB, all six domains) live in the session scratchpad
under `/private/tmp`, which is prunable. The two complete `healthcare` arms are ~2.5 h of GPU time
that exists in exactly one prunable place. This programme has already lost a checkout to exactly
that, and the fix is to relocate the tree before resuming rather than after.

## Method delta from the parent page

None. Literally none — which is the point of recording it.

- Executor `openai/gpt-oss-20b` on LM Studio `:4242`, scripted user (no user-simulator variance),
  `mode_fc` / `dep_full` / `fmt_structured` / `tool_full`, 1 run per task, full task set per domain.
- Four arms: `none` · `--constraint_hint` · `--action_order` · `--scaffold pva`.
- **Each arm has its own `--output_dir`.** This is the trap the parent page documents — the output
  filename encodes model/mode/dep/fmt/tool/shuffle but **not** the scaffold or ablation flag, so
  arms sharing a directory silently resume each other and produce four "arms" that are one arm.
- **A 1-task pre-flight ran per domain** into a throwaway directory and was asserted to contain a
  real scored interaction before any full spend, specifically to catch a silent all-fail that still
  exits zero.
- Scored by SOPBench's `evaluator_function_directed_graph` through its own `try_eval`. **No LLM
  anywhere in the scoring loop.** Pairing is the intersection of tasks present in all four arms;
  significance is exact McNemar on the discordant cells.

Per-task scored vectors are committed as `evals/sopbench-bank-asop/T2-<domain>-scored.json`,
alongside the three the parent page already carries. Raw trajectories (~5 MB/arm) stay in the
session scratchpad.

## `hotel` — n=195 paired (the full task set; 195/195 in every arm, zero exclusions)

| arm | **success** | verification complete (`dirgraph`) | per-call permissible | final-state match | correct action call |
|---|---|---|---|---|---|
| `none` | **0.226** | 0.415 | 0.621 | 0.574 | 0.554 |
| `constraint_hint` | **0.277** | 0.441 | 0.641 | 0.605 | 0.600 |
| `action_order` | **0.328** | 0.821 | 0.631 | 0.595 | 0.605 |
| `pva` | **0.426** | **0.903** | **0.928** | **0.933** | **0.749** |

Paired against `none`, exact McNemar:

| arm | delta | 95% CI | gained | lost | p |
|---|---|---|---|---|---|
| `constraint_hint` | +0.051 | [−0.028, +0.130] | 36 | 26 | 0.2529 |
| `action_order` | **+0.103** | **[+0.014, +0.191]** | 50 | 30 | **0.0330** |
| `pva` | **+0.200** | **[+0.120, +0.280]** | 55 | 16 | **0.0000** |

The headline replicates a fourth time: **baseline worst, PVA best, significant.** `dirgraph_satisfied`
moves 0.415 → 0.903, the mechanism visibly doing the thing it claims.

### 🛑 The split does **not** replicate here, and this is the most important row on the page

| arm | permissible (n=69) | impermissible (n=126) | avg tool calls |
|---|---|---|---|
| `none` | 0.043 | 0.325 | 1.84 |
| `constraint_hint` | 0.116 | 0.365 | 2.21 |
| `action_order` | **0.304** | 0.341 | 4.10 |
| `pva` | 0.072 | **0.619** | 2.80 |

**PVA moves the permissible half by 3 tasks out of 69** (0.043 → 0.072) while nearly doubling the
impermissible half (0.325 → 0.619). Every prior domain could say "both halves rise" — `bank`
0.167→0.333, `online_market` 0.267→0.450, `library` 0.042→0.333. **`hotel` cannot.**

This matters because "both halves rise" is the parent page's stated evidence that the gain is real
work rather than a refusal bias, and it is the specific claim that distinguishes this result from
the parent project's measured tau2 liability. On `hotel` that evidence is **absent for PVA**: the
permissible half is flat within noise, and essentially all of PVA's +0.200 is bought on tasks where
the correct answer is to refuse.

Be precise about what this does and does not say:

- It is **not** over-gating in the strict sense. Over-gating would show the permissible half
  *falling*. It did not fall; it rose by 3 tasks.
- But it is **not** the "both halves rise" pattern either, and a page that reported only the
  aggregate +0.200 would have hidden that.
- `action_order` is the permissible-half winner here (0.304 vs PVA's 0.072) — the reverse of the
  aggregate ranking — and it pays **4.10 calls/task**, the most of any arm in this domain and 2.2×
  baseline, to get there. On accuracy gained per tool call it lands at 0.025, barely ahead of
  `constraint_hint`'s 0.023 and far behind PVA's 0.071. (The parent page's claim that the
  compiled-order arm is the *worst* value-per-call does **not** generalise — measured across four
  domains it is worst on two and `constraint_hint` is worst on the other two. What does hold 4/4 is
  that **PVA is the best**.)

`hotel` also has the lowest baseline of any domain measured (0.226, and 0.043 on the permissible
half — the executor completes almost nothing it should complete). Whether the flat permissible half
is a property of the domain or a floor effect of a weak executor **is not resolved by this data**,
and the capability-gradient arm that would resolve it is still unrun.

## `healthcare` — paused, not scored

⏸ Two arms complete, two partial. See the resume point in Status. Deliberately unscored; a
partial-arm intersection would be a structured subset, not a sample.

## `dmv` — not run

⏸ Never started.

## `university` — not run

⏸ Never started. **Pre-registered caveat, recorded before the numbers exist so it cannot read as
post-hoc:** `university` has **42 task specs**, against 195 for `hotel` and 124 for `healthcare`.
With a permissible/impermissible split it will carry roughly 15/27 per cell. At `hotel`'s effect
size the paired test on 42 tasks is underpowered, and **a null on `university` should be read as
"not measurable at this n", not as "the effect is absent there".** A significant result at that n
would have to be large.

## Cross-domain reading — four of seven domains

With `hotel` in, the ladder has now been measured on **four domains: `bank`, `online_market`,
`library`, `hotel`** (n = 134 + 172 + 66 + 195 = **567 paired tasks**). What survives four domains:

| finding | replicated? |
|---|---|
| baseline is the worst arm, `pva` the best | ✅ **4/4** |
| `pva` beats baseline significantly | ✅ **4/4** (p = 0.0013 / 0.0000 / 0.0000 / 0.0000) |
| `dirgraph_satisfied` rises most under `pva` | ✅ **4/4** (to 0.791 / 0.860 / 0.939 / 0.903) |
| `pva` beats `action_order` on success | ✅ **4/4** (0.642>0.634, 0.709>0.541, 0.561>0.364, 0.426>0.328) |
| `pva` has the best accuracy-gain-per-tool-call | ✅ **4/4** (0.062 / 0.087 / 0.072 / 0.071) |
| `pva` raises the **permissible** half too | ❌ **3/4 — `hotel` breaks it** (+3 tasks of 69) |
| the two middle rungs keep their order | ❌ **2–2 split** — `order`>`hint` on `bank`/`hotel`, `hint`>`order` on `online_market`/`library` |
| `action_order` is the worst value-per-call | ❌ **2/4** — it is worst on `online_market`/`library`, but `constraint_hint` is worst on `bank`/`hotel` |

**The endpoints are solid and the interior is noise.** Four domains is enough to stop hedging on
"procedure guidance beats baseline and PVA is the best arm" — that is as replicated as anything in
this programme. It is *not* enough to rank `constraint_hint` against `action_order`, and four
domains have now failed to do so in four different ways.

🛑 **The one claim that four domains weakened rather than strengthened is the one the parent page
leans on hardest.** "Both halves rise, therefore this is real work and not a refusal bias" held on
three domains and **failed on the fourth**. On `hotel` the entire +0.200 is bought on the
impermissible half. The honest statement is now:

> PVA reliably improves aggregate success and verification completeness. **Whether it improves
> correct *completion* is domain-dependent** — it did on three domains and did not on `hotel`.

That is a materially weaker claim than "not over-gating", and it is the claim the remaining three
domains would have tested. They are the reason to finish the sweep later, not a formality.

## What this page does not settle

Everything the parent page's "What this does NOT settle" section lists, unchanged and still
binding: it is **not** a test of our ASOP (PVA is SOPBench's scaffold in SOPBench's vocabulary);
one executor, and a weak one; no adversarial user; service-desk domain class throughout; single run
per task at temperature 0, so the CIs are across tasks, not across repeated runs of a task.

## Reproducing

Identical to the parent page's Reproducing section. The driver used here is
`scratchpad/overnight.sh` + `scratchpad/run_arm.sh` (session-local, not in-repo): one domain at a
time, four arms in parallel (the GPU saturates at 4 concurrent streams), each with its own
`--output_dir`, gated on a 1-task pre-flight.

```bash
LADDER_DIR=<ladder-root> .venv/bin/python evals/sopbench-bank-asop/score_ladder.py <domain>
```

Both `swarm/constants.py` registrations were verified present before the runs
(`OPENAI_MODELS` at `:34` and `FUNCTION_CALLING_MODELS["openai"]` at `:213`).
