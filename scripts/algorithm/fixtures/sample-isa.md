---
task: "synthetic ISA proving the ISC-1 compiler, no real domain"
slug: 20260917-000000_isc1-compiler-proof
effort: standard
effort_source: explicit
phase: build
progress: 0/5
mode: iterate
started: 2026-09-17T00:00:00Z
updated: 2026-09-17T00:00:00Z
---

# ISC-1 compiler proof — synthetic ISA

> Not a real procedure. Exists to exercise `scripts/algorithm/isa_to_asop.py` against
> all three gate kinds plus the one honest-downgrade case, per the principal's scoping
> call (2026-09-17): build the compiler first, prove it against a synthetic ISA, decide
> the real target domain separately.

## Goal

Prove that an ISA's `## Criteria` compiles to valid asop-spec gates without any
per-criterion human judgment at compile time.

## Criteria

- [ ] ISC-1: **Authenticate before mutating.** A tool lookup returns the user's id before any write. — verified by `find_user_id` returning ok.
- [ ] ISC-2: **Confirm before an irreversible action.** The user explicitly confirms before the agent cancels or deletes anything. — verified by a named reviewer deciding whether confirmation was obtained.
- [ ] ISC-3: **The refund total is arithmetically correct.** No named tool computes this; it is a property of the final response text. — verified by inspecting the response for a correct total.
- [ ] ISC-4: **The test suite passes before any release.** A raw shell command re-run in CI. — verified by `run_command`.
- [ ] ISC-5: **A file's format is well-formed.** Present to prove refusal on ambiguity — this line names no test and has no Test Strategy row.

## Test Strategy

| ISC | Type | Probe | Expected | Tool |
|---|---|---|---|---|
| ISC-1 | functional | Call `find_user_id` before the first write | tool returns ok | run_command |
| ISC-2 | judgment | Ask whether the user confirmed the cancel/delete | yes, explicitly | human |
| ISC-3 | judgment | Read the final response for the refund total | total matches order records | judged |
| ISC-4 | functional | `uv run pytest -q` | exit 0 | run_command |

_(ISC-5 deliberately has no row above — proves the compiler refuses rather than
guesses on an under-specified criterion.)_
