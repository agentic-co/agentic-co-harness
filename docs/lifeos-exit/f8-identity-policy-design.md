# F8(i) — denylist/safety-classifier as identity-policy config (design, not code)

**ISC:** `lifeos-exit/ISA.md` ISC-8, half (i). **Status: DRAFTED** — design only.
`agentco_harness/` is another agent's lane tonight; nothing here is implemented.

Source read (read-only): `LIFEOS/DOCUMENTATION/Security/README.md`,
`hooks/lib/safety-classifier.ts` (referenced, not modified), `EMBEDDED_RUNTIME_PLAN.md` §Components,
`ai-tasks/unified/phase-3.md`.

## Why this can't be "ported" as code

Today's enforcement is three Claude-Code-specific layers (`Security/README.md`):

- **L1** — a sentence in `LIFEOS_SYSTEM_PROMPT.md` telling the model that external content is
  data, not instruction. This is a property of *the model reading a particular prompt*.
- **L2** — `settings.json`'s `permissions.deny` block: literal/glob patterns Claude Code's harness
  checks before any model decision (`rm -rf /`, `curl|sh`, force-push to main, credential-file
  reads, …).
- **L3** — `hooks/Safety.hook.ts` + `hooks/lib/safety-classifier.ts`: a `PermissionRequest` hook
  that classifies outgoing tool calls (`DANGEROUS_PATTERNS`, `CREDENTIAL_PATHS`,
  `INJECTION_SHAPES`, a trusted-workspace allowlist, a dev-binary allowlist, a read-only-tool
  allowlist) and a `PostToolUse` hook that tags incoming web content.

All three fire only because the executor happens to be the Claude Code CLI: `PermissionRequest`
and `PostToolUse` are Claude Code hook events, `settings.json` is a Claude Code config file, and
the system prompt is a Claude Code concept. `ai-tasks/unified/phase-3.md` already names the
consequence as an open gap for this front (ISC-4): *"it permits hooks to become ASOP checks
without requiring equivalent protection in ungated hosts. The assistant and intake hosts lose that
enforcement entirely unless it is specified."* Porting the TypeScript hook files verbatim would
special-case exactly one backend — the thing ISC-8 and the ISA's own Principles section
("enforcement must be backend-agnostic") explicitly rule out.

## What moves, and where it lands

`EMBEDDED_RUNTIME_PLAN.md` §Components already names the target: **component 4, Identity
policy** — *"authenticated principal, declared human/verifier/adjudicator authority, executor
route identity, per-run bindings. A backend label is not a credential."* The denylist/
safety-classifier logic is additional identity-policy content: not "who is allowed to approve
this gate" but "what shape of action is this executor route allowed to take at all."

**Move the catalogs, not the hook.** L2's pattern list and L3's four catalogs
(`DANGEROUS_PATTERNS`, `CREDENTIAL_PATHS`, `INJECTION_SHAPES`, plus the allow-side lists —
read-only tools, dev binaries, trusted-workspace paths, pre-vetted adapter calls) become
**declared registries**: plain versioned data (YAML/JSON), not code, not tied to any hook event.
This repo already has a working precedent for exactly this pattern outside the runtime-enforcement
path: `Security/README.md`'s **release deny-list** (`~/.claude/skills/_LIFEOS/DENY_LIST.txt`) is
"plain text, one ripgrep-compatible regex per line... adding a pattern: append the regex line...
no code to edit." The design below is that same idea, applied to runtime action-shape checks
instead of release-time secret scanning.

Two registries, each carrying a `registry_version` field — the same field name the ISA's ISC-2
provenance envelope already uses for its attribute registry, kept consistent across the estate
rather than inventing a second versioning convention:

- **`refused_shapes`** — supersedes `DANGEROUS_PATTERNS` + `CREDENTIAL_PATHS` +
  `INJECTION_SHAPES`. Each entry: a pattern (regex or structured shape descriptor), a category
  (`destructive` / `credential-access` / `injection` / …), and a severity (`refuse` outright vs.
  `escalate` to a human-authority check). Content-equivalent to today's three catalogs; shape
  changes so it's data the identity-policy component evaluates, not TypeScript a hook imports.
- **`allowed_shapes`** — supersedes the classifier's allow branches (pre-vetted adapter calls,
  read-only operations, dev-binary invocations, trusted-workspace path targets). Matching this
  registry short-circuits to "proceed without escalation"; it exists so routine, low-risk actions
  don't all funnel into a human-authority check just because nothing marked them dangerous.

## Fail-closed, restated precisely

Today's classifier's *unmatched* default is `neutral` — "native engine prompts" (a human is there
to answer). That's already fail-toward-asking, which is correct for an interactive Claude Code
session. It is **not yet fail-closed** for every executor route: an unattended `agy`/Codex bead or
a headless dispatch has no human to prompt, so a `neutral` result with nobody watching silently
degrades to whatever that executor's own default happens to be — which may be "proceed."

**The identity-policy design property, stated as a rule an evaluator enforces regardless of
executor route:** an action shape matching neither registry is **refused**, not proceeded with and
not silently escalated-and-forgotten. A human-attended route may *convert* that refusal into an
escalation (surface it and wait); an unattended route has no such conversion available and the
refusal stands. This is the concrete meaning of "declared registries, fail-closed" in ISC-8's
wording — the registries are declarative data either way, but the executor-route context (attended
vs. unattended — itself part of "Identity policy"'s existing scope, "executor route identity")
determines whether a no-match becomes a wait or a hard stop, and a hard stop is the only default
that holds for every route including ones nobody is watching.

## What stays out of identity-policy, deliberately

- **L1 (the constitutional prompt sentence)** does not port. It's a property of which model reads
  which system prompt; a registry can't make a model "read external content as data." Each
  executor adapter remains responsible for its own equivalent instruction to its own model. What
  identity-policy *can* do — and what L3's `PostToolUse` annotate() path already does today — is
  tag suspicious shapes on ingress as a visibility aid (`INJECTION_SHAPES`-style detection folded
  into `refused_shapes`' `injection` category, surfaced as a marker rather than a hard block, since
  ingress content isn't an action to refuse). That tagging is backend-agnostic and belongs in the
  registry-evaluation path; the model-level judgment it supports does not.
- **The release-time deny-list** (`DENY_LIST.txt`) is a separate, already-correct mechanism for a
  separate problem (don't publish identity/secrets in a public release) and needs no change here —
  cited above only as evidence the declared-registry pattern already works in this codebase.
- **Re-deleted complexity.** `Security/README.md`'s own "What's NOT Here" section lists
  PatternInspector, EgressInspector, RulesInspector, PromptInspector, InjectionInspector,
  SmartApprover-as-a-framework, and their supporting docs — all deleted because they were "regex
  scaffolding" duplicating judgment the model already has, or duplicating what a narrower
  mechanism (L2's literal deny-list) already covers. The identity-policy design above should not
  reconstitute any of these: `refused_shapes`/`allowed_shapes` are the narrow, single-purpose
  successor to the specific two lists (L2 + L3's classifier) that earned their keep, not a
  reopening of the inspector framework the LifeOS team already tried and cut.

## What's still unimplemented (flagged, not built here)

ISC-4's acceptance probe — "the same rule producing the same refusal under at least two different
executor adapters" — has no home to run against yet: there is no identity-policy module in
`agentco_harness/` today (confirmed by this session's F9 probe: 0 hits for `denylist` inside any
module that isn't just prose about environment-variable allowlisting), and no executor adapter
call site consults a shape registry before dispatch. This design names the target shape; wiring
`executor.py`'s dispatch paths through it is unimplemented work for whoever owns that lane.
