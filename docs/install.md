# Install and first run

Written for a person or a coding agent to follow step by step. Every step has a
check. If a check fails, stop and read [Troubleshooting](#troubleshooting)
instead of improvising.

**If you are a coding agent asked to set this up:** do sections 1–3 in order and
record each command's output. Do not install the `[lm]` extra, do not run
`agentic-co daemon`, do not set a hub URL, token or API key, and do not create
schedules or recurring tasks on a first run. If `doctor` reports something
other than the findings listed in section 3, report it as written; do not
"fix" it.

## What this is

`agentic-co` is one Python package. It keeps a local file of tasks ("beads"),
runs a cycle that dispatches them to a headless agent CLI, verifies the result,
and tells you what is broken (`doctor`). It needs no server, no hub and no
account beyond the agent CLI you already use.

## Requirements

| Need | Check |
|---|---|
| Linux, macOS, or WSL2 on Windows | Native Windows fails at import (the store uses `fcntl`). Use WSL2. |
| Python 3.11 or newer | `python3 --version` |
| `uv` | `uv --version` (install below) |
| `git` | `git --version` (the install fetches this repository) |
| Network access to `github.com` | The repository is public; no token is needed. |
| Claude Code (`claude`) on `PATH` and logged in | `claude --version`. Only needed to have beads *executed* by the `claude` backend. Install, `init` and `doctor` work without it. |

No `uv` yet:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
```

## 1. Install

```sh
uv tool install "git+https://github.com/agentic-co/agentic-co-harness"
agentic-co --help
```

Check: `agentic-co --help` prints a command list that includes `init`,
`tasks`, `cycle` and `doctor`.

Without `uv` (needs Python 3.11+):

```sh
python3 -m pip install --user "git+https://github.com/agentic-co/agentic-co-harness"
export PATH="$HOME/.local/bin:$PATH"
agentic-co --help
```

The installed script is `agentic-co`. (`agentco` is a different, older
product.)

## 2. Create a node

A node is a directory holding `config.yaml` and `tasks.jsonl`. Use a directory
of your own, not an existing project.

```sh
mkdir -p ~/work-node && cd ~/work-node
agentic-co init --company
```

Check: it prints `Created config.yaml` and `Created tasks.jsonl`, then creates
`company/` (a folder skeleton; it reports `Registered 21 documents`) and
`.agentco/`. The live task file is `tasks.jsonl` in this directory; the
`tasks.jsonl` inside `.agentco/` stays empty.

Plain `agentic-co init` creates only `config.yaml` and `tasks.jsonl`. It is
the minimal form, and `doctor` then reports `company/` missing (section 3).
Running `agentic-co init --company` afterwards also works and keeps your
existing `config.yaml`.

Two defaults in the generated `config.yaml` worth knowing:

- `llm.default_provider` is `openai`. That only matters for in-process model
  calls; beads dispatched to `claude` use that CLI's own login.
- `notify.enabled` is `true` and points at `http://localhost:31337/notify`. If
  you have no notification service there, set `notify.enabled: false`.

## 3. Run doctor

```sh
agentic-co doctor; echo exit=$?
```

Exit codes: `0` clear, `1` something is BROKEN, `2` something is DEGRADED.

**A node made with `init --company` exits 2 today**, with exactly these two
findings:

| Finding | What it says | Read it as |
|---|---|---|
| `DEGRADED llm.provider_key` | The default provider has no API key. | Only affects in-process model calls. |
| `DEGRADED egress.policy` | No `inference-routes.json`. Every non-Anthropic route is denied; Anthropic (`claude`) dispatch is unaffected. | Expected until you configure other vendors. |

A node made with plain `init` exits 1: it adds `BROKEN company.dir` (`company/`
is missing, so agent documents would be discarded). That message tells you to
run `agentco init --company`; the command in this package is
`agentic-co init --company`.

Any other `BROKEN` or `DEGRADED` line is new information. Report it as printed.

`doctor` does **not** check that `claude` is installed. If it is missing, the
first bead that needs it fails with
`claude binary 'claude' not found on PATH — cannot execute task`. Run
`claude --version` yourself.

## 4. A first task

Needs `claude` installed and logged in.

```sh
agentic-co tasks create "Write ok to result.txt" \
  -d "In the current directory create a file named result.txt containing exactly the word ok. Do nothing else." \
  --agent claude --task-class agent --timeout 300 --max-turns 10 \
  --verify-check 'test "$(cat result.txt)" = ok'
agentic-co cycle
agentic-co tasks list
```

A bead needs an executor: `--agent claude` as above, or `--assign human:<name>`
for a person. A bead with neither makes `doctor` report
`BROKEN queue.dispatchability`.

`--verify-check` is the bead's definition of done: a bead only reaches `done`
when that shell command exits 0. If the bead does not reach `done`, run
`agentic-co tasks show <id>` and `agentic-co attention`; they say why. Then
`agentic-co runs`, `agentic-co usage` and `agentic-co cost` show what the run
recorded.

Run `agentic-co <command> --help` before using a flag you have not seen here.

## Leave alone on a first run

`agentic-co daemon`, schedules and recurring tasks, and the hub (`hub.url`) are
all optional. Nothing in sections 1–4 needs them.

## Sharing what happened

To hand a run to someone else for review without sending task text, use the
metadata-only export: [`docs/audit.md`](audit.md).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `agentic-co: command not found` | `~/.local/bin` is not on `PATH`: `export PATH="$HOME/.local/bin:$PATH"`. |
| `ModuleNotFoundError: fcntl` at startup | Native Windows. Run it inside WSL2. |
| Python is older than 3.11 | `uv` can fetch one: `uv python install 3.12`, then reinstall. Or use a newer system Python. |
| The install cannot reach `github.com` | Network or proxy policy. Nothing else here works around it; carry a built wheel to the machine instead (`uv build --wheel` from a clone). |
| `claude binary 'claude' not found on PATH` | Claude Code is not installed or not on `PATH` for the shell that runs `agentic-co`. |

Contributors: the editable-install and test steps are in the
[README](../README.md#install).
