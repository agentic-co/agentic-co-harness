# asop.claude.v6 (retail) — what changed and why

**Kept OUT of the ASOP itself deliberately**, same reasoning as v3's sidecar: the
document's preamble reaches the agent's context, so an explanation this long inside
it would confound a v3-vs-v6 comparison with ~200 words of prose the agent reads,
on top of the one line that is the actual treatment.

## Starting point

Started from `asop.claude.v3.md` verbatim (its title line still read "v2" — a leftover
from the v2→v3 handoff that only ever touched gate wording; fixed here to "v6" since
this is now a different document, at zero cost to the word budget). Read
`asop.claude.v3.NOTES.md` first, per the brief, and did not touch anything it flags as
intentional — in particular the `find_user_id` prefix (accepts `_by_email`/`_by_name_zip`
on purpose) and the liveness/correctness distinction on every gate.

## The one substantive change

**Finding N14** (`EXTRACTION-PROMPT.md:71-75`): the four ASOP properties — ordering,
preconditions, prohibitions, completion — are all about constraint and stopping.
None asks what the agent should ACCOMPLISH. Measured consequence: zero-write rate
tracks how ASOP-shaped the document is (8% prose vs 15-23% for v2/v4/v5), same tool-call
and turn counts — the agent doesn't stall, it abandons before the mutating step.

Added one new global rule, **G8**, placed last in the Global Rules list (after G7 —
Transfer, before Reference Data):

> **G8 — Finish the request.** Once preconditions are met, carry the procedure to its
> final step and artifact — do not stop at authentication, a lookup, or confirmation
> alone.

Why this wording and this placement:

- **Global Rules, not a new procedure step.** A step inside a numbered procedure would
  change `_steps_in`'s step count for that procedure and violate "identical parse
  shape." Global Rules is a bullet list (`- **Gn — ...`), which `parse_asop` only
  promotes to a `Procedure` when it can read numbered steps out of it — it can't here,
  so this section rides in the preamble exactly as G1-G7 already do. Confirmed: both
  static checks report the same 15 declared / 15 firing / 10 discriminating / 5
  siblings / 0 nothing / 0 false-accept as v3, meaning no gate's parse changed.
- **Named the three concrete stopping points from N14's own measurement**
  (authentication, a lookup, confirmation) rather than writing a generic "try harder"
  line — the failure mode is agents completing every step up to but not including the
  mutating tool call, so the rule names that shape specifically. This is a stance
  about finishing, deliberately not a pep talk ("do your best", "be helpful") — it reads
  in the same register as G1-G7, each one line, each starting with the imperative.
- **`Gate:` was deliberately omitted from G8.** Every other Gn either has one (G1, G3,
  G7) or doesn't need one because it's a standing constraint rather than a checkable
  step (G2, G4, G5, G6). G8 is the same shape as G2/G4/G5/G6: a standing disposition
  that only manifests inside the procedures' own steps and gates. Giving it a fake
  `Gate:` line to look consistent would have been exactly the "document about when to
  stop" pattern this rule exists to counter, and would have risked `named_tool`
  matching stray text as a 16th deterministic gate.

## Word budget

- v3: 2109 words (`wc -w`).
- v6: 2139 words (`wc -w`) — **+30, at the edge of the requested ±30 band, not over it.**
- The addition itself is 29 words including the "G8 —" label; the title fix (v2→v6)
  is a wash (one word swapped for one word). Nothing was cut elsewhere — the budget
  closed without needing to, so no other content was touched, which also minimizes the
  diff against v3 to exactly two lines (see below).

## Verification

Both required static checks pass and reproduce v3's numbers exactly:

```
$ python3 scripts/eval/gate_reach.py evals/tau2-retail-asop/asops/asop.claude.v6.md
TOTAL  15 of 15 declared deterministic gates can fire.
(exit 0)

$ python3 scripts/eval/gate_probe.py --tools "calculate,cancel_pending_order,exchange_delivered_order_items,find_user_id_by_email,find_user_id_by_name_zip,get_order_details,get_product_details,get_user_details,list_all_product_types,modify_pending_order_address,modify_pending_order_items,modify_pending_order_payment,modify_user_address,return_delivered_order_items,think,transfer_to_human_agents" evals/tau2-retail-asop/asops/asop.claude.v6.md
TOTAL  10 discriminating · 0 checking nothing · 5 accepting siblings · 0 false-accepting (of 15 declared)
(exit 0)
```

`diff asop.claude.v3.md asop.claude.v6.md` shows exactly two changes: the title line
and the new G8 bullet. No gate line, tool name, step, or procedure was touched.

## What EXTRACTION-PROMPT.md and the v3 NOTES say that bears on this brief

- `EXTRACTION-PROMPT.md:71-75` confirms the brief's premise exactly: the four
  mandated properties (ordering, preconditions, prohibitions, completion) are all
  about constraint/proof-of-doneness, never about the goal. No contradiction found —
  this is the cleanest possible confirmation, not a conflict.
- `EXTRACTION-PROMPT.md` rule 3 says "stay close in length... a large expansion means
  content was added, which breaks rule 1 [preserve the content]." That rule governs
  prose-to-ASOP *extraction*, not this v3-to-v6 edit, but it's the same spirit as the
  brief's ±30-word budget, and G8 doesn't add domain content — it's a disposition
  about existing procedures, not a new rule about carts, refunds, or addresses — so it
  doesn't run afoul of "nothing new may appear" in the sense that rule cares about.
- The v3 NOTES' warning that "these are LIVENESS gates, not correctness gates" and the
  `find_user_id` prefix explanation are both still accurate of v6 unchanged — nothing
  in this edit touches either concern. No disagreement found between the brief and
  either file.
