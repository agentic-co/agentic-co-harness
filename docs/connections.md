# Connections and instance identity: spec (owner-approved direction, 2026-11-13)

## Goal
One machine runs several harness instances, for example a personal one and one for a client
company. Each instance must use ONLY the model accounts and GitHub identity it was given, and must
never fall back to a machine-wide default.

The owner sets connections up the way other agent harnesses do: `agentic-co connect` shows a
selector of connection kinds, collects the details, **validates the connection live**, and saves
it. Each instance's config then names which connections it uses.

## Connection kinds (v1 scope, owner's words)
| kind | how many per machine | what it is |
|---|---|---|
| `claude_code` | many | A Claude Code login. `auth: subscription` (a `claude setup-token` token) or `auth: api_key`. Each has its OWN `config_dir`. |
| `agy` | at most ONE | The Antigravity CLI, using its single saved login under $HOME. The registry refuses a second one. |
| `openai_compat` | many | Any OpenAI-compatible chat-completions endpoint: LiteLLM, z.ai, a local server. Fields: `base_url`, `model`, optional key. |
| `github` | many | A git/GitHub identity: `ssh_key`, `gitconfig` (user.name and email), `gh_config_dir`, optional `expect_github_user`. |

## Registry (machine-level, never in a repo)
- Location: `$AGENTCO_CONNECTIONS_DIR`, else `~/.config/agentco/connections/`. The directory is mode 0700.
- One file per connection, `<name>.json`, mode 0600. Name: `^[a-z0-9][a-z0-9-]{0,62}$`.
  ```json
  {"schema": "agentco.connection/1", "name": "acme-claude", "kind": "claude_code",
   "auth": "subscription", "config_dir": "~/.claude-acme",
   "secret_file": "~/.config/agentco/connections/acme-claude.secret",
   "validated_at": "2026-11-13T22:40:00Z", "validated": "ok: claude replied in 4.1s"}
  ```
- Secrets live ONLY in a separate `secret_file` (mode 0600), never inline in the JSON and never in env
  files or launchd plists. The loader refuses a secret file whose mode is looser than 0600.
- Public API (`agentco_harness/connections.py`):
  - `connections_dir() -> Path`
  - `load(name) -> Connection` (a frozen dataclass per kind or one with optional fields; your call)
  - `list_all() -> list[Connection]`
  - `save(conn, secret: str | None)`, `remove(name)`
  - `read_secret(conn) -> str | None`
  - `validate(conn) -> ValidationResult(ok: bool, detail: str)`, which runs a LIVE probe:
    - `claude_code`: run `claude --print "Reply with exactly: OK" --max-turns 1` with the env from
      `child_env(conn)`, and check the output contains OK.
    - `agy`: `agy --print "Reply with exactly: OK" --dangerously-skip-permissions --print-timeout 60s`.
    - `openai_compat`: one chat completion with max_tokens 5, using stdlib urllib as `completion.py`
      does. Fall back to GET `/models` only if the owner asks for a no-cost check.
    - `github`: `ssh -T git@github.com` with the pinned key (exit code 1 plus "Hi <user>!" means ok).
      Compare with `expect_github_user` when it is set.
  - `child_env(conn) -> dict[str, str]`: the env ADDITIONS for a subprocess using this connection.
    - `claude_code` + `subscription`: `CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_OAUTH_TOKEN`. The caller
      MUST also strip `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`, because those win over the
      token silently.
    - `claude_code` + `api_key`: `CLAUDE_CONFIG_DIR`, `ANTHROPIC_API_KEY`; strip
      `CLAUDE_CODE_OAUTH_TOKEN`.
    - `github`: `GIT_SSH_COMMAND="ssh -i <key> -o IdentitiesOnly=yes -o IdentityAgent=none"`,
      `GIT_CONFIG_GLOBAL=<gitconfig>`, `GH_CONFIG_DIR=<dir>`. `SSH_AUTH_SOCK` is never passed.
    - `agy`: nothing extra (it uses $HOME).
    - `openai_compat`: no subprocess env; `completion.Provider` is built from it.

## Instance config (each harness instance's config.yaml)
```yaml
identity:
  name: acme
  claude: acme-claude            # a claude_code connection
  github: acme-github            # a github connection
  completions: [acme-litellm]    # openai_compat connections this instance may use
  agy: agy                         # optional; only one exists per machine
```
- **Fail-closed.** If `identity:` is declared and a named connection is missing, of the wrong kind,
  or has an unreadable secret, the bead is REFUSED with a coded refusal. There is never a fallback
  to `~/.claude/.env`, `ANTHROPIC_API_KEY_UNATTENDED`, `~/.ssh/config` or the default `~/.claude`.
- **No `identity:`** means today's behaviour (single-instance machines keep working), and `doctor` warns.
- **`doctor`** checks: every named connection exists and is the right kind; secret files are 0600;
  the last validation is recent, or it runs `connections.validate` with `--live`; no two instances
  on this machine share a `claude_code.config_dir` or a `github.ssh_key` (the instance list comes
  from the registered children, if available; otherwise skip with a note).

## CLI (`agentic-co connect`)
- `agentic-co connect`: an interactive selector of kind, with prompts for that kind's fields
  (`claude setup-token` output pasted with hidden input), then a live validation, then save only on ok.
  `--no-validate` saves anyway and marks it unvalidated.
- `agentic-co connect list`: name, kind, auth, validated_at and status. No secrets, ever.
- `agentic-co connect test <name>`: re-runs validation and updates `validated_at`.
- `agentic-co connect remove <name>`: deletes the JSON and secret files after confirmation.
- Everything must be scriptable with flags, so tests and automation never need a TTY.

## Non-goals (v1)
- GitHub Copilot, and multiple agy accounts.
- Keychain integration for secrets. The 0600 file is the v1 store.
