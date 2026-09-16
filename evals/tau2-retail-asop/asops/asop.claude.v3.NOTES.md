# asop.claude.v3 (retail) — what changed and why

**Kept OUT of the ASOP itself deliberately.** The document's preamble reaches the
agent's context, so a note this long inside it would make a v2-vs-v3 comparison
differ by ~220 words of prose as well as by the gate names — two variables, and
this document exists to isolate one.

**v3 — 2026-09-15. The only change from v2 is that every `deterministic` gate now
NAMES the tool it re-checks.** v2 declared 15 deterministic gates and *none* of them
could fire: `named_tool` needs a snake_case identifier in the gate text, and "tool
lookup returns a user id" names no tool, so all 15 silently downgraded to a model
judge. The document looked rigorous and every "deterministic" number it produced was
an opinion. Verify with `python3 scripts/eval/gate_reach.py <this file>` before running.

⚠️ **These are LIVENESS gates, not correctness gates.** What fires is
`check_tool_succeeded`: did this tool run, and did it return ok. A gate on
`get_order_details` does NOT verify the status is *pending* — only that the lookup
happened. Per `ai-tasks/unified/phase-2.md`, a liveness result must never be reported
as a correctness result; recall 0.43 is a fact about the correctness class. Tag every
verdict row with its class before scoring.

**`find_user_id` is a deliberate prefix, not a typo.** Authentication succeeds via
either `find_user_id_by_email` or `find_user_id_by_name_zip`; `check_tool_succeeded`
matches the tool name as a substring of the call line, so the shared prefix accepts
both. Naming one would refuse every conversation that used the other. If that
substring match is ever tightened to an exact match, this line breaks — and it should
break loudly rather than start refusing valid authentications.
