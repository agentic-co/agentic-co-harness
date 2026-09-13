# Does a bigger judge make the gate work?

No. Not within anything this machine can run.

## The question

T1 found the gate refuses *less* often on preconditions that demonstrably
failed than it does on average — a detector running backwards. Two
explanations survived:

- **(a)** gates on judged business rules cannot discriminate, full stop;
- **(b)** a 20B model judging its own kind of reasoning cannot, and a better
  judge would.

## The method

Every recorded gate decision carries the evidence its verifier saw, so the
judge can be swapped with everything else held identical — same transcripts,
same tool histories, same prompts, same deterministic labels. No agent, no user
simulator, no environment. 192 decisions, replayed through each judge.

**LIFT** is the measurement: catch rate on demonstrably-failed preconditions,
minus that judge's own base refusal rate. Recall alone is worthless — a judge
that refuses everything scores 1.00 — and precision is unavailable because the
rule-labelled set is single-class. A judge refusing at random scores lift 0
however good its catch rate looks.

## The result

| judge | params | refuses | catches | **lift** |
|---|---|---|---|---|
| gpt-oss-20b | 20B dense | 0.58 | 0.48 | **−0.10** |
| gemma-4-31b | 31B dense | 0.84 | 0.88 | **+0.04** |
| qwen3-coder-next | 80B MoE (~3B active) | 0.82 | 0.73 | **−0.08** |

**A four-fold span of parameters moves lift by 0.14 and never above +0.04.**
Two of the three are negative. The best result is indistinguishable from a
judge that refuses at random, and it comes from one refusing 84% of everything
— which would stall any run it gated.

The numbers replicate. An earlier pass with a different message shape gave
gpt-oss −0.09 and gemma +0.05 against −0.10 and +0.04 here. This is not noise.

**Bigger did not mean better.** The 80B MoE scored *worse* than the 31B dense
model. Sparse models activate a few billion parameters per token, so file size
is not capability for a reasoning task — and it is coder-tuned, which does not
help with refund eligibility.

## What it does and does not settle

It rules out the easy version of (b): more capability *within the local range*
does not rescue the gate. It does not rule out a frontier judge, which could
not be tested — the available API key had no balance.

It says nothing about C2 or C3. Those were never in this experiment.

And it is a finding about **judged** gates on **this domain's** rules. Airline
policy is mostly assertions about state that no command can re-run, which is
why 55% of these gates degraded to a model opinion in the first place. The
deterministic path exists in the adapter and never fired on these documents,
because they declare `deterministic (tool call)` without naming a tool.

## What follows

Testing C1 where a gate can actually *be* a gate — a software domain where the
check is `run the tests` and needs no judge at all. If gates cannot
discriminate even when they are literally executable, the verification claim is
in serious trouble. If they can, the finding is sharp and publishable: **gate
mechanisms work; model-judged gates on ambiguous operational rules do not.**

## Reproducing

```
scripts/eval/t1_rejudge.py --tau2 <checkout> --worksheet <worksheet> \
    --models openai/openai/gpt-oss-20b openai/google/gemma-4-31b
```

Load one model at a time; two of these do not fit in memory together. Send the
judge prompt as a USER turn — several chat templates reject a system message
with no user turn, and the failure is a 400 that looks like a model problem.
