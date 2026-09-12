# The extraction prompt

Given **verbatim and identically** to every extractor. It is the control: if the
prompt differed between them, a difference in their output would tell us nothing
about the models and everything about the instructions.

Reproduced here so anyone can re-run the extraction with a model we did not use,
and so a reader can check for themselves whether it smuggles in the answer.

---

You are converting a customer-service policy document into an **ASOP** — an
Agentic Standard Operating Procedure.

## Your input

One file: `policy.v0-prose.md`. It is the operating policy for an airline
customer-service agent, written as prose.

**That file is your only source.** Do not add domain knowledge from anywhere
else. Do not invent rules. Do not soften or strengthen a rule. If the prose is
ambiguous, keep the ambiguity rather than resolving it — resolving it is a
change, and changes are what we are trying not to make.

## What an ASOP is

A versioned, ordered sequence of steps that accomplish one type of task. Each
step states:

- **what must be done** — the action, in the imperative;
- **preconditions** — what must be true before the step may start;
- **how the result is proven** — the gate. One of:
  - `deterministic` — a check that can be re-run and gives the same answer,
  - `judged` — an independent party forms a verdict,
  - `human` — a person signs off;
- **where the result is written** — the artifact or state the step produces;
- **which role does it** — who acts.

A procedure without those properties is documentation, not an ASOP.

## What to produce

For each distinct procedure in the input, an ASOP with ordered steps. Make
explicit the things prose leaves implicit:

- **ordering** — what must happen before what;
- **preconditions** — the checks the prose states as rules, attached to the step
  they actually gate;
- **prohibitions** — things that must be refused, and at which step;
- **completion** — what makes a step actually done, rather than attempted.

Pay attention to any rule the prose says the system will NOT enforce for the
agent. Those are exactly the preconditions worth making explicit, because
nothing else will catch them.

## Rules of the exercise

1. **Preserve the content.** Every rule in the input must survive. Nothing new
   may appear. A reader holding both documents should be able to trace each rule
   from one to the other.
2. **Restructure, do not rewrite.** Keep the original wording wherever it still
   fits. You are changing the *shape*, not the prose style.
3. **Stay close in length.** The input is ~1,300 words. Structure costs some
   words; a large expansion means content was added, which breaks rule 1.
4. **Do not address a specific scenario.** You have not been shown any task this
   will be tested on, and you must not write as though you had. Write the
   procedure for the domain, not for an example.

## Output

Markdown only — the ASOP itself, nothing else. No preamble, no explanation of
your approach, no commentary. The file is going into a system prompt verbatim.
