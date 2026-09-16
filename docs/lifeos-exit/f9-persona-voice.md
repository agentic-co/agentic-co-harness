# F9 — persona/voice confirmed frontend-only

**ISC:** `lifeos-exit/ISA.md` ISC-9. **Status: probe run, hits found, none load-bearing, script
now gates on them — see per-hit read below. Re-run:**
`bash docs/lifeos-exit/probe-f9-persona-voice.sh` (exits 1 if a hit isn't in the reviewed
allowlist, 0 otherwise).

The ISA's literal probe expects 0 matches for both greps. The real run found matches for both.
Per the task instructions: nothing here was ripped out. What follows is what matched, where, and
why each one is not the thing ISC-9 is trying to catch — plus one caveat already on record in
`ai-tasks/unified/phase-3.md` that bears directly on how much weight this probe can carry at all.

**Update:** the script originally exited 0 unconditionally, which made it useless as a gate — a
hit could sit unread forever. It now carries an explicit `KNOWN_BENIGN` allowlist of the exact
substrings analyzed below and exits 1 on anything not in it — a genuinely new persona/voice/
denylist reference (the thing ISC-9 guards against) fails the script; today's reviewed hits stay
green. Probe 2's `persona` term is also now word-boundaried (`\bpersona\b`), which was needed
regardless of the exit-code fix — it was matching `personal`/`impersonated` as substrings, which
inflated the hit count with lines that aren't about persona at all. Counts below are corrected
accordingly (my first pass under-counted Probe 2 by one: 15 raw hits, not 14).

## Probe 1 — `leeloo|elevenlabs|voice` (case-insensitive), expect 0

Five hits, two source lines:

- `agentco_harness/orchestrator.py:198,244` — `"[Leeloo owes] ..."`. This is a bead-title
  naming convention, not a persona feature: the orchestrator's `agent`-class task budget exists
  because the principal's own operational rules say a deferred DA commitment gets filed as a bead
  titled `[Leeloo owes] ...`. The code branches only on `metadata.task_class == "agent"`
  (`orchestrator.py:272`) — never on the literal string `"Leeloo"`. Confirmed by
  `rg -n "task_class" agentco_harness/orchestrator.py`: the only executable dependency is the
  generic `task_class` field; `"Leeloo"` appears exclusively inside comments explaining *why* the
  class exists. Deleting the comment's example name changes nothing at runtime.
- `agentco_harness/notify.py:6,62` and `agentco_harness/config.py:305` — the word "voice"/"voiced"
  describing the **Pulse** endpoint (`NotifyConfig.url`, default
  `http://localhost:31337/notify`) as an external, opaque webhook target. Checked the full
  `NotifyConfig` dataclass (`config.py:300-315`): it has exactly `enabled`, `url`,
  `telegram_chat_id`, `telegram_token_env`, `cycle_summary`. No voice ID, no ElevenLabs key, no
  persona field, no speech parameter of any kind. The harness POSTs a plain message to a URL; that
  Pulse *chooses* to speak it aloud is a property of the receiving system, invisible to and
  uncontrolled by the harness.

## Probe 2 — `\bpersona\b|voiceId|denylist`, expect 0 outside an identity-policy module

**Raw pattern as the ISA wrote it (`persona|voiceId|denylist`, no word boundary): 15 hits.**
Seven of those are the unrelated English word **"personal"** substring-matching `persona`
(`cli.py:2024`'s `--task-class personal` choice, `config.py`'s "personal pipeline", `children.py`
x2's "personal nodes"/"personal vanished", `egress.py`'s `== "personal"` check) and one is
"**impersonated**" (`beads.py`: "a hostile or impersonated plane") — none of these are about
persona at all; they're a regex substring accident. **Word-boundaried (`\bpersona\b`): 8 hits**,
all genuine word matches, none of them the DA/voice concern ISC-9 names:

- **"persona" = code-producer executor nickname**, not DA persona (2 distinct lines, 3 hits:
  `executor.py:925,1011`, `orchestrator.py:1590`). `"""Execute a bead via Google's Antigravity CLI
  (the Bellows persona)."""` / `"...OpenAI's codex CLI (the Forge persona)."""`. In this LifeOS
  deployment, "Bellows"/"Forge"/"Anvil"/"Temper" are docstring nicknames for which vendor CLI a
  function shells out to (see the Agent-tool roster in this session: Bellows = Google/agy, Forge =
  OpenAI/codex). This is the harness's own existing vocabulary for backend selection, unrelated to
  assistant identity/voice.
- **"denylist" = subprocess environment allowlist framing**, not a security/safety-classifier
  denylist (5 hits: `executor.py:185,211,280,283`, `beads.py` — line number shifts as that file is
  concurrently edited by another lane; matched by content, not line number). All read some
  variant of "an allowlist applied to \[...\] spawn paths is a denylist" — this is `_clean_env()`'s
  documentation of a bug class (secrets leaking into subprocess environments because an allowlist
  wasn't applied everywhere), not identity-policy's denylist/safety-classifier concept named in
  ISC-8.

Word-boundarying `persona` does not affect `voiceId` or `denylist` — neither term collides with an
unrelated word in this codebase, so only the `persona` half of the pattern needed the fix.

## Also checked: the ISA's ISC-8 test-strategy grep

The Test Strategy table also names `grep -r "persona|voiceId|denylist" agentco_harness/ agentco/`
for ISC-8. `agentco/` does not exist in this repo (`find . -maxdepth 1 -type d` — the Hub package
lives in the separate `agentco-hub`/`agentic-co-hub` repos, out of this lane's reach). Recorded so
the gap isn't silently assumed away by whoever runs ISC-8's own probe later.

## Caveat already on record — this probe is weak evidence either way

`ai-tasks/unified/phase-3.md` already flags this: *"ISC-9 is downgraded as a proof. `grep -ri
"voice"` returning zero proves the absence of a string, not that the interface is decoupled. The
real risk is a voice-shaped field on an evidence record — that needs a schema check, not a
grep."* This cuts both directions: the clean result I'd want to report doesn't exist (the grep
isn't 0), but even a clean grep wouldn't have proven decoupling. The thing actually worth checking
— whether any persistence/evidence schema in `agentco_harness/` carries a voice-ID-shaped or
persona-shaped field — I did check by reading the one config dataclass a "voice" hit pointed at
(`NotifyConfig`) and it has none. No other dataclass in `config.py` or record shape in `beads.py`
was implicated by either grep, so there's nothing else to schema-check from this evidence alone.

## Recommendation

No code change needed for ISC-9 as currently understood: the DA's actual voice/persona
configuration (voice IDs, personality sliders, autonomy toggles) lives in three places, all
outside this repo and all host/frontend surfaces — `~/.claude/settings.json`'s `daidentity` block
(JSON, read by Claude Code hooks), `LIFEOS/USER/CONFIG/LIFEOS_CONFIG.toml`'s `[da]`/`[da.voices.*]`
tables, and `LIFEOS/USER/DIGITAL_ASSISTANT/DA_IDENTITY.md`'s prose body — never
`agentco_harness/`.

The `persona` word-boundary fix is now implemented in `probe-f9-persona-voice.sh` (it was a real
false-positive generator, independent of anything else). The `voice`/`voiced` distinction I
originally floated as a possible further tightening was **not** applied: "voiced" and "voice +"
are handled instead by the `KNOWN_BENIGN` allowlist (they're reviewed, documented, and pass), which
is the more honest fix — a regex refinement to exclude "voiced" would also hide a *future* line
that genuinely does encode a voice ID inside a comment using the word "voice", where the allowlist
approach only ever passes text that has actually been read and classified. The allowlist is the
mechanism that should absorb future review, not further pattern-narrowing.

**Whether "0 matches" is the right criterion, stated plainly (asked for explicitly in the
follow-up):** no, not as literally written, and this isn't a case for rewording the grep — it's
the same conclusion `ai-tasks/unified/phase-3.md` already reached for a different reason (grep
proves absence-of-string, not decoupling). Two independent failure modes point the same way:
phase-3.md's — a clean grep can still hide a voice-shaped field — and this session's — a dirty
grep can be entirely non-substantive, because English words collide with a security-adjacent
vocabulary (`persona` as an executor nickname, `denylist` as an env-var allowlist term, `voice` as
a description of an unrelated webhook). **The criterion that survives both problems is a reviewed
allowlist gate, not a bare match count** — exactly what the script now implements: report every
hit, classify each once against a human-reviewed list, fail only on the unclassified remainder.
That's a rewrite of the ISC-9 *test*, not the *property* — the property ("no component encodes
personality/voice ID/persona") still holds; only the mechanical proxy for checking it needed to
change.
