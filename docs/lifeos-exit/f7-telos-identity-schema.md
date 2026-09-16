# F7 — TELOS/identity/operational-rules as portable data

**ISC:** `lifeos-exit/ISA.md` ISC-7. **Status: DRAFTED** — schema documented, gap identified, tiny
worked example added at `docs/lifeos-exit/f7-example/`. No real personal content copied into this
repo; every value below and in the example is fabricated.

Source read (read-only, not copied): `~/.claude/LIFEOS/USER/` — a symlink to
`~/.config/LIFEOS/USER/` (confirmed via `ls -la`; documented as deliberate in
`OPERATIONAL_RULES.md`'s "Second machine" section) — specifically `TELOS/`, `PRINCIPAL/`,
`DIGITAL_ASSISTANT/`, `CONFIG/`, and `PROJECTS.md`, plus
`LIFEOS/DOCUMENTATION/Freshness/FreshnessSystem.md` and `hooks/lib/identity.ts`.

## (a) The portable contract

Two independent layers, and only one of them is genuinely portable data today.

### Layer 1 — file location + frontmatter (portable, already data)

Every constitutional file lives at a **fixed relative path** and carries a flat YAML frontmatter
block using the `pai-freshness-v1` convention (`FreshnessSystem.md`):

```yaml
---
last_updated: <ISO-8601 timestamp>
last_updated_by: <free-form string — author/agent/tool name>
convention: pai-freshness-v1
last_reviewed: <ISO-8601 timestamp>       # optional
last_reviewed_by: <free-form string>       # optional
provenance: user | template | principal    # optional, seen in the wild, not in the spec doc
derived_from: <path>                       # only on generated derivatives
generator: <path>                          # only on generated derivatives
---
```

Required keys for a file to participate in freshness tracking: `last_updated`,
`last_updated_by`, `convention`. `last_reviewed`/`last_reviewed_by` are optional — their absence
means "never reviewed," a valid state (grades to `F`), not an error. A derived file additionally
carries `derived_from` + `generator` and is explicitly exempted from driving its own staleness
signal — the reviewed-ness of the *source* is what counts (`FreshnessSystem.md`, "Auto-generated
derivatives").

This is the entire structured contract. It is generic YAML — no custom types, no LifeOS-specific
tags — parseable by any frontmatter reader (a five-line regex split on the first `---...---`
block plus a stock YAML parser, demonstrated in `f7-example/probe_bare_read.py`).

### Layer 2 — the body (portable, but deliberately NOT machine-schema'd)

Below the frontmatter, every file is **plain Markdown prose**, meant to be read whole — by an LLM
loaded via `@`-import, or by a human. The only convention inside the body is Markdown headings
(`##` per topic) plus, in `TELOS.md` specifically, an optional per-section HTML-comment marker:

```markdown
## Mission
<!-- updated: 2026-07-18 by:Leeloo -->
```

This marker is greppable (for per-section freshness) but **not required** — a consumer that
ignores it still gets the correct content; it only loses the finer-grained staleness signal. No
other file uses per-section markers. A consumer should not assume any other embedded structure
(bullet lists, bold labels like `**M0:**` — these are human/LLM-readability conventions, not a
schema to parse against). Treating the body as "prose, read whole" is what F7's probe (bare
`Read`, no parser) actually validates.

### File inventory (fixed paths, relative to a `USER/` root)

| Path | Required? | Purpose |
|---|---|---|
| `TELOS/TELOS.md` | required for TELOS | Single source of truth: Current State, Ideal State, Mission, Problems, Strategies, Narratives, Challenges, Beliefs, Wisdom, Books (per `TELOS/README.md`'s file table — some of those live as separate files, some as `##` sections inside `TELOS.md`, both are valid per the real install). |
| `TELOS/PRINCIPAL_TELOS.md` | required, but **derived** | Auto-generated flat summary of `TELOS.md`; this is the file actually `@`-imported into session context. Consuming it needs only a bare read; *regenerating* it after an edit to `TELOS.md` needs the LifeOS tool (see gap below). |
| `TELOS/CURRENT_STATE/*.md`, `TELOS/IDEAL_STATE/*.md` | optional | Per-dimension detail (Health, Finances, Work, Relationships, Learning, Energy). Ship empty/template in a fresh install — genuinely optional, not a hidden requirement. |
| `TELOS/LIFEOS_STATE.json` | optional | Plain JSON, `{dimensions: {name: {pct, tbd_count, source_file, last_updated}}}` — a dashboard-percentage cache derived from the `CURRENT_STATE`/`IDEAL_STATE` split. Not part of the identity/rules contract proper; noted for completeness. |
| `PRINCIPAL/PRINCIPAL_IDENTITY.md` | required | The principal's identity, role, worldview — freeform prose under conventional `##` headings. |
| `DIGITAL_ASSISTANT/DA_IDENTITY.md` | required | The assistant's identity/voice/personality **as prose** — see the frontmatter-schema gap below; the populated file does not use the structured schema the template documents. |
| `CONFIG/OPERATIONAL_RULES.md` | optional | Principal-specific operational rules; ships as a stub, grows by appending `##` sections — no schema beyond that. |
| `PROJECTS.md` | optional | A registry table (project/path/URL/deploy/stack) plus a routing-alias table — both are ordinary Markdown tables, parseable only in the loose sense that Markdown tables are; not consumed by code as structured data anywhere found in this repo or in LifeOS's own hooks. |

## (b) Where a LifeOS-specific parser is actually required today — and what replaces it

**Finding: the documented frontmatter schema for `DA_IDENTITY.md`/`PRINCIPAL_IDENTITY.md` is dead
code against the real, populated files.**

`~/.claude/LIFEOS/USER/DIGITAL_ASSISTANT/_example/identity.md` (the template new users copy) and
`DIGITAL_ASSISTANT/README.md` document a **nested, structured** frontmatter contract —
`id`, `name`, `display_name`, `color`, `role`, `personality.traits.*`, `voice.voice_id`,
`autonomy.can_initiate`/`must_ask` — described as "read by hooks via `lib/identity.ts`."

`hooks/lib/identity.ts` (`getIdentity()`, `getPrincipal()`) does implement a parser for exactly
that nested shape: `fm.core.name`, `fm.voice.main.voice_id`, `fm.personality.traits.*`,
`fm.core.startup_catchphrase`, `fm.core.pronunciation`/`timezone`, `fm.preferences`,
`fm.tech_stack`.

But the **real, populated** `DA_IDENTITY.md` and `PRINCIPAL_IDENTITY.md` (read directly, not
copied here) carry only the flat `pai-freshness-v1` keys shown in Layer 1 above — no `core:`,
`voice:`, or `personality:` keys at all. Every field name is prose in the body instead (e.g. a
bold-labeled line naming the ElevenLabs voice ID and voice name in running text — real value
elided here, it is not needed to make the point and this repo does not carry the principal's
actual identifiers). So `loadDaFrontmatter()`'s nested-schema fallback
path in `identity.ts` returns `{}` for every field it looks up against a real install, and
`getIdentity()` never actually reaches that fallback in practice anyway, because its **first**
read point is `~/.claude/settings.json`'s `daidentity` block (confirmed: that file has top-level
`daidentity: {mainDAVoiceID, name, voices}` and `principal` keys) — a Claude-Code-specific JSON
settings file, not a LifeOS USER data file at all. A **third** copy of the same values (voice IDs,
names) lives in `LIFEOS/USER/CONFIG/LIFEOS_CONFIG.toml`'s `[da]`/`[da.voices.main]`/`[principal]`
tables, marked there as "canonical source... system code reads these through
`LIFEOS/TOOLS/PaiConfig.ts`."

So there are, in the live system, **three divergent representations** of the same DA identity
facts (name, voice ID): a documented-but-unused Markdown-frontmatter schema, a Claude-Code
`settings.json` JSON block that's actually read first, and a `LIFEOS_CONFIG.toml` table claimed as
canonical. None of the three is "the portable contract" on its own — and per this repo's own
ISC-9 (persona/voice is frontend-only, `docs/lifeos-exit/f9-persona-voice.md`), none of them
*should* be: voice IDs and personality sliders are exactly the assistant-host-specific runtime
config ISC-9 keeps out of the Harness/Hub core.

**What this means for F7's contract, concretely:** the parser dependency (`hooks/lib/identity.ts`
plus its `settings.json`/`LIFEOS_CONFIG.toml` fallback chain) is not something a future consumer
needs to replace to satisfy ISC-7, because the fields it exists to extract (voice ID, personality
sliders, autonomy toggles as machine-actionable settings) are not part of the portable
TELOS/identity contract at all — they're assistant-host runtime config, out of scope here by
ISC-9's own boundary. What a future consumer *does* need, and what this contract promises, is
narrower and already satisfied by Layer 1 + Layer 2: fixed path, freshness frontmatter, prose
body. Nothing in `agentco_harness/` needs to parse `core.name` or `voice.main.voice_id` out of a
Markdown file, because nothing in `agentco_harness/` should be reading DA voice config from
anywhere (ISC-9). The only thing a future host needs from these files is exactly what a bare
`Read` gives it.

**One real regeneration dependency, correctly scoped:** editing `TELOS.md` and wanting
`PRINCIPAL_TELOS.md` back in sync requires running `LIFEOS/TOOLS/GenerateTelosSummary.ts` — a
LifeOS-specific tool. This is NOT a violation of ISC-7: the criterion is about *loading*
(consuming) the files with a bare read, which `PRINCIPAL_TELOS.md` already satisfies as a static
artifact at read time. A future core only needs its own equivalent "flatten TELOS sections into a
summary" step if it wants auto-regeneration; until then, treating `PRINCIPAL_TELOS.md` as just
another portable file (edited by hand or regenerated by whatever tool the new system chooses) is
sufficient.

## (c) Worked example

`docs/lifeos-exit/f7-example/` — a tiny, fully fabricated file set (fictional principal "Alex
Doe", fictional assistant "Nova") reproducing the shapes above: `TELOS/TELOS.md`,
`TELOS/PRINCIPAL_TELOS.md` (derived, carries `derived_from`/`generator`), `PRINCIPAL/
PRINCIPAL_IDENTITY.md`, `DIGITAL_ASSISTANT/DA_IDENTITY.md`, `CONFIG/OPERATIONAL_RULES.md`,
`PROJECTS.md`. `probe_bare_read.py` in that directory loads every file with nothing but
`pathlib.Path.read_text` + a hand-rolled ~10-line frontmatter splitter (no `pyyaml`, no LifeOS
import) and asserts the three required keys are present and the body is non-empty markdown —
proving ISC-7's "loading them with a bare file read, no LifeOS-specific parser required" against
the shape documented above, not just against prose claiming it.
