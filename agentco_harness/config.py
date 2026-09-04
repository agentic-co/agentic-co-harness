"""Configuration management."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# --- Environment file -------------------------------------------------------
#
# LifeOS keeps secrets in `~/.claude/.env`, which Claude Code sources at session
# start. AgentCo runs under launchd with a near-empty environment and never read
# that file, so a key that works interactively is simply absent in the daemon.
# That asymmetry burned four days of feed ingests: ZAI_API_KEY was present in
# `.env` the whole time while every `Ingest youtube:` bead failed with "z.ai API
# key not found" — a hard failure wearing an intermittent one's clothes, which
# is exactly the silent-failure class this project exists to kill.
#
# Loading it here makes both paths agree. The ISA's "secrets come from env,
# never config files" invariant is preserved rather than weakened: `.env` is an
# env file, not config, and it is never parsed as config, never round-tripped
# through `save()`, and never held on a Config field where it could be logged.
DEFAULT_ENV_FILE = "~/.claude/.env"

# Set AGENTCO_ENV_FILE to point at a different env file, or to "" to skip the
# load entirely (tests and CI, which must run with no keys present at all).
ENV_FILE_OVERRIDE_VAR = "AGENTCO_ENV_FILE"


def load_env_file(path: Path | str | None = None) -> list[str]:
    """Merge `KEY=VALUE` pairs from an env file into `os.environ`.

    Returns the names (never the values) of the keys it set, so callers can log
    what was picked up without leaking secrets.

    Precedence: **the real environment always wins.** A key already present in
    `os.environ` is left untouched, so a plist `EnvironmentVariables` entry, a
    shell export, or a test's monkeypatch can still override the file. This is
    the safe direction — the file is a fallback for the daemon, not an authority
    that silently overwrites a deliberate override.

    Failure posture matches the rest of the project: a missing file is a normal
    no-op (not every deployment is a LifeOS install), and a malformed line is
    warned-and-skipped rather than fatal — a fat-fingered `.env` must never take
    down a cycle.
    """
    override = os.environ.get(ENV_FILE_OVERRIDE_VAR)
    if override is not None:
        if not override.strip():
            return []  # explicitly disabled
        path = override
    elif path is None:
        path = DEFAULT_ENV_FILE

    env_path = Path(path).expanduser()
    if not env_path.exists():
        return []

    try:
        text = env_path.read_text()
    except OSError as e:
        print(f"[config] WARNING: cannot read env file {env_path} ({e}) — skipping")
        return []

    loaded: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            print(
                f"[config] WARNING: skipping malformed line {env_path}:{lineno} "
                f"(no '=' — expected KEY=VALUE)"
            )
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            print(f"[config] WARNING: skipping line {env_path}:{lineno} (empty key)")
            continue
        value = _strip_env_value(value.strip())
        if key in os.environ:
            continue  # real environment wins
        os.environ[key] = value
        loaded.append(key)
    return loaded


def _strip_env_value(value: str) -> str:
    """Unwrap a quoted env value, or trim a trailing `# comment` from a bare one.

    Comment-stripping is deliberately limited to unquoted values with whitespace
    before the `#`, so a `#` inside a secret (perfectly legal in a token) is not
    silently truncated into a key that fails auth for a mysterious reason.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    head, sep, _ = value.partition(" #")
    return head.rstrip() if sep else value


@dataclass
class HubConfig:
    """A coordination plane this runtime participates in (ASOP.md §7, decision 8).

    Off when `url` is unset. `actor` is the name this runtime pulls and
    reports as — the binding label a plane run names when it means this
    node. The secret is read from the environment, never from this file.
    """

    url: str | None = None
    actor: str = "harness"
    secret_env: str = "AGENTCO_HUB_SECRET"
    timeout_s: int = 30
    lease_ttl_s: int = 3600

    @property
    def enabled(self) -> bool:
        return bool(self.url)


@dataclass
class EgressConfig:
    """Where the data-classification route table is read from.

    ``routes_path`` is an absolute path to an exported route table. Left
    unset, `egress.artifact_path` resolves `inference-routes.json` beside the
    bead store — a file this runtime owns, rather than one inside a
    particular operator's home directory.
    """

    routes_path: str | None = None


# Agent settings keys that are actually consumed by the codebase.
# Anything else in an agent's config block triggers a loud warning at load.
CONSUMED_AGENT_KEYS = {"model", "use_claude_code", "context", "description"}

# Top-level config keys the loader understands.
KNOWN_TOP_LEVEL_KEYS = {"tasks_path", "agents", "llm", "triage", "notify", "instance", "humans", "tiers", "backoff", "executor", "capabilities", "egress", "hub"}

#: Blocks the v1 hub consumed that this runtime deliberately does not. They
#: configured pipelines that belonged to one operator — a feeds ingester
#: pointed at an Obsidian vault, a fixed set of polled inboxes. The Harness
#: replaces both with `register_source_factory`, so an integration brings its
#: own configuration rather than negotiating for a field in this file.
#:
#: Named rather than merely unknown, because "unknown top-level key 'feeds'"
#: reads as a typo when it is actually a removal, and the operator who sees
#: it is holding a config file that used to work.
RETIRED_TOP_LEVEL_KEYS = {
    "feeds": (
        "the feeds ingester was a personal pipeline; register a source "
        "factory instead (see agentco_harness.orchestrator."
        "register_source_factory)"
    ),
    "sources": (
        "polled sources are supplied by extensions now; register a source "
        "factory instead (see agentco_harness.orchestrator."
        "register_source_factory)"
    ),
}

# The model-tier registry (Delegation Layer, Stage 2). Capable models plan and
# route; small models execute atoms. A subtask's `metadata.executor_tier` resolves
# through this table at claude dispatch; the planner bead runs on tiers["planner"].
# `local` is DEFERRED on purpose (same class as the z.ai routing deferral, ISA
# Out of Scope) — it is intentionally absent, and naming it in config warns.
DEFAULT_TIERS = {
    "planner": "claude-fable-5",     # judgment, decomposition, routing
    "worker": "claude-sonnet-5",     # multi-step execution
    "executor": "claude-haiku-4-5",  # atomic, pattern-match, single-file
}
KNOWN_TIER_KEYS = frozenset(DEFAULT_TIERS)

# Nested-block keys the loader actually consumes (each derived from the `.get(`
# calls in Config.load below). Anything else inside a block is silently dropped
# unless we warn — same loud-drop posture as unknown top-level and agent keys.
# `zai_api_key` is included in the llm set even though the current loader does
# not yet read it: another branch is adding that read, and warning on it now
# would be a false positive.
CONSUMED_LLM_KEYS = {"default_provider", "default_model", "api_key", "base_url", "zai_api_key"}
CONSUMED_TRIAGE_KEYS = {"model", "api_base", "api_key"}
CONSUMED_NOTIFY_KEYS = {"enabled", "url", "telegram_chat_id", "telegram_token_env", "cycle_summary"}
CONSUMED_HUMANS_KEYS = {"enabled", "escalate_to"}
CONSUMED_BACKOFF_KEYS = {"enabled", "base", "factor", "max"}
CONSUMED_EXECUTOR_KEYS = {"idle_timeout_s"}

# Stalled-builder watchdog default: 15 minutes of TOTAL silence from a worked
# agent's subprocess. Long enough that a slow test suite or a long single tool
# call never trips it, short enough that a hung builder is caught inside one
# hourly cycle instead of burning the whole execution budget.
DEFAULT_IDLE_TIMEOUT_S = 900


def _warn_unknown_nested(block_name: str, block: Any, consumed: set[str], path: Path) -> None:
    """Warn (loudly, once) about keys inside a nested block that nothing reads."""
    if not isinstance(block, dict):
        return
    unknown = set(block) - consumed
    if unknown:
        print(
            f"[config] WARNING: block '{block_name}' has key(s) nothing consumes: "
            f"{', '.join(sorted(unknown))} in {path} "
            f"— they are ignored (consumed keys: {', '.join(sorted(consumed))})"
        )


@dataclass
class AgentConfig:
    model: str = "gpt-4o-mini"
    settings: dict[str, Any] = field(default_factory=dict)

    @property
    def context(self) -> str:
        """Operator-written context for this agent, injected into its prompts."""
        return self.settings.get("context", "") or ""


@dataclass
class LLMConfig:
    default_provider: str = "openai"
    default_model: str = "gpt-4o-mini"
    api_key: str | None = None
    base_url: str | None = None
    zai_api_key: str | None = None  # z.ai (Zhipu AI) cloud key — set or use ZAI_API_KEY env


@dataclass
class TriageConfig:
    """The cheap model that triages the open queue each heartbeat cycle.

    Model strings are DSPy-style `provider/model`. Pointing triage at an
    OpenAI-compatible local endpoint (e.g. LM Studio) is a pure config
    change: model `openai/<model>` + api_base `http://localhost:1234/v1`.
    """

    model: str = "anthropic/claude-haiku-4-5"
    api_base: str | None = None
    api_key: str | None = None


@dataclass
class NotifyConfig:
    """Best-effort external notification.

    `url` is the Pulse endpoint (voiced — urgent events only).
    `telegram_chat_id` enables direct Telegram Bot API messages; the bot
    token comes from the env var named by `telegram_token_env` and never
    lives in this file. `cycle_summary` sends a Telegram message after
    every heartbeat cycle.
    """

    enabled: bool = True
    url: str = "http://localhost:31337/notify"
    telegram_chat_id: str | None = None
    telegram_token_env: str = "TELEGRAM_BOT_TOKEN"
    cycle_summary: bool = False


@dataclass
class TiersConfig:
    """Model-tier registry: tier name → model string.

    Consumed at claude dispatch (a task's `metadata.executor_tier` resolves to a
    model via `model_for`) and by the planner bead (runs on `model_for("planner")`).
    A tier the registry does not know resolves to None — the caller then warns and
    falls back to the CLI default model (advisory degradation, triage doctrine).
    """

    models: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TIERS))

    def model_for(self, tier: str | None) -> str | None:
        """Model string for a tier, or None if the tier is unset/unknown."""
        if not tier:
            return None
        return self.models.get(tier)


@dataclass
class HumansConfig:
    """People as first-class executors (delegation layer, Stage 1).

    `enabled` gates human assignment. When False, `tasks create --assign`
    refuses loudly rather than silently — a parsed-but-unconsumed config value
    is a lie (project Principle). Flipping it False is the Stage-1 kill-switch:
    it stops NEW human assignment; existing `human:` tasks stay excluded from
    ready() and visible in `me`.
    """

    enabled: bool = True
    # `human:<name>` that automated escalations (exhausted RCA loops) land on.
    # Unset = "human:operator", a placeholder the operator renames on setup.
    escalate_to: str | None = None


@dataclass
class BackoffConfig:
    """Adaptive cycle backoff — an *advisory* gate on top of the fixed launchd
    cadence. launchd still wakes the instance on its fixed interval (hourly for
    portfolio, daily for feeds); this block decides whether that wake actually
    runs a cycle or exits fast.

    While a queue is active (open beads, new beads, a due recurring def, or an
    explicit --force) every wake runs and the interval sits at `base`. Once the
    queue goes genuinely idle, the interval doubles each idle cycle by `factor`
    up to `max`, so a dormant project is checked ever less often — spreading
    effort onto the companies that are actually moving.

    Advisory posture (mirrors triage/notify): a malformed block never fails a
    cycle. It is warned loudly, `agentco doctor` FAILs on it, and backoff falls
    back to disabled (every wake runs at baseline) — never a silent skip.

    `base`/`max` are `parse_duration` strings ('1h', '2h', '1d', '7d'); `factor`
    is the idle multiplier (must be > 1 to make progress).
    """

    enabled: bool = True
    base: str = "1h"
    factor: float = 2.0
    max: str = "7d"

    def validation_errors(self) -> list[str]:
        """Return a list of human-readable problems; empty means valid.

        Used by both the cycle gate (to decide advisory fall-back) and by
        `agentco doctor` (to FAIL loudly) — one source of truth for what
        'malformed' means, so the two can never disagree.
        """
        from .recurring import parse_duration  # local import avoids a cycle

        errors: list[str] = []
        base_s = max_s = None
        try:
            base_s = parse_duration(self.base).total_seconds()
        except (ValueError, TypeError) as e:
            errors.append(f"base={self.base!r} is not a valid duration ({e})")
        try:
            max_s = parse_duration(self.max).total_seconds()
        except (ValueError, TypeError) as e:
            errors.append(f"max={self.max!r} is not a valid duration ({e})")
        try:
            factor = float(self.factor)
            if factor <= 1.0:
                errors.append(f"factor={self.factor!r} must be > 1 to make progress")
        except (ValueError, TypeError):
            errors.append(f"factor={self.factor!r} is not a number")
        if base_s is not None and max_s is not None and max_s < base_s:
            errors.append(f"max ({self.max}) is smaller than base ({self.base})")
        return errors


@dataclass
class ExecutorConfig:
    """Subprocess-boundary behaviour for worked agents.

    ``idle_timeout_s`` is the stalled-builder watchdog: if the child process
    shows no sign of life for this many seconds, it is terminated and the bead
    FAILS. It is deliberately a *silence* budget, not a wall-clock one — the
    wall clock is already covered by ``budget.timeout``, and the failure this
    exists to kill is the builder that is neither working nor exiting, which no
    wall-clock budget catches until the budget is fully burned.

    "Sign of life" means stream bytes OR a write to the child's own session
    transcript. Stream bytes alone are not enough: the store-backed path runs
    the CLI with ``--output-format json``, which emits nothing until exit, so a
    stdout-only watchdog degenerates into a hard wall-clock cap that silently
    overrides ``budget.timeout`` — it killed two consecutive ingest beads at
    exactly 900.0s (RCA ac-d82a660f). See ``executor._TranscriptProbe``.

    ``0`` disables the watchdog entirely (back to pre-watchdog behaviour: a
    silent child runs until the overall timeout). A negative or non-integer
    value is warned about at load and falls back to the default — advisory
    degradation, same posture as backoff/triage.
    """

    idle_timeout_s: int = DEFAULT_IDLE_TIMEOUT_S


@dataclass
class Config:
    """AgentCo configuration."""

    tasks_path: str = "tasks.jsonl"
    
    agents: dict[str, AgentConfig] = field(default_factory=lambda: {
        "cs": AgentConfig(model="gpt-4o-mini"),
        "pm": AgentConfig(model="gpt-4o"),
        "dev": AgentConfig(model="claude-sonnet-4-20250514"),
        "devops": AgentConfig(model="gpt-4o"),
        "analyst": AgentConfig(model="gpt-4o-mini"),
    })
    
    llm: LLMConfig = field(default_factory=LLMConfig)
    triage: TriageConfig = field(default_factory=TriageConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    egress: EgressConfig = field(default_factory=EgressConfig)
    hub: HubConfig = field(default_factory=HubConfig)
    humans: HumansConfig = field(default_factory=HumansConfig)
    tiers: TiersConfig = field(default_factory=TiersConfig)
    backoff: BackoffConfig = field(default_factory=BackoffConfig)
    executor: ExecutorConfig = field(default_factory=ExecutorConfig)
    # --- capability manifest (ac-39d4dbc8) ----------------------------------
    # What THIS node can do — the lane declaration matched against a bead's
    # `requires` at claim time. Empty (the default) means the node declares
    # nothing and may therefore claim only unrestricted beads.
    #
    # This is a manifest, not a grant: writing `ado-write` here does not create
    # a credential, it asserts that this machine already holds one. The two must
    # be kept true together, which is exactly why the declaration is a visible
    # line in config.yaml rather than something inferred from the environment —
    # inferring it would make the lane silently follow whatever secret happened
    # to be in scope, and the whole point is that the write PAT never leaves the
    # MacBook (Plans/TwoMachineLifeos.md, invariant 2).
    capabilities: list[str] = field(default_factory=list)
    instance: str | None = None  # instance name for heartbeat; defaults to queue dir name
    config_path: str | None = None  # absolute path this config was loaded from; None if defaults

    @property
    def recurring_path(self) -> str:
        """recurring.jsonl lives beside the task queue."""
        return str(Path(self.tasks_path).parent / "recurring.jsonl")

    @property
    def children_registry_path(self) -> str:
        """children/registry.jsonl lives beside the task queue."""
        return str(Path(self.tasks_path).parent / "children" / "registry.jsonl")

    @property
    def heartbeat_path(self) -> str:
        """heartbeat.json lives beside the task queue."""
        return str(Path(self.tasks_path).parent / "heartbeat.json")

    @property
    def runs_path(self) -> str:
        """runs.jsonl (structured execution log) lives beside the task queue."""
        return str(Path(self.tasks_path).parent / "runs.jsonl")

    @property
    def asops_path(self) -> str:
        """asops.jsonl (the local ASOP store) lives beside the task queue."""
        return str(Path(self.tasks_path).parent / "asops.jsonl")

    @property
    def store_dir(self) -> str:
        """The directory holding the bead store.

        Anything the runtime keeps beside its queue resolves from here, so
        that a second instance in a second directory is a second everything
        and not two instances sharing one file by accident.
        """
        return str(Path(self.tasks_path).parent)

    @property
    def instance_name(self) -> str:
        return self.instance or Path(self.tasks_path).resolve().parent.name

    @classmethod
    def load(cls, path: Path | str = "config.yaml") -> "Config":
        """Load config from YAML file.

        Relative tasks_path resolves against the config file's directory,
        never the process working directory.

        Loading config is also where the env file is merged in, before any
        provider key is read — every entry point (cycle, feeds, doctor, CLI)
        already goes through here, so the daemon and an interactive shell see
        the same secrets without each command remembering to bootstrap them.
        """
        load_env_file()

        path = Path(path)
        if not path.exists():
            return cls()

        with open(path) as f:
            data = yaml.safe_load(f) or {}

        config = cls()
        config.config_path = str(path.resolve())

        for key in data:
            if key in RETIRED_TOP_LEVEL_KEYS:
                print(
                    f"[config] WARNING: '{key}' in {path} is no longer read — "
                    f"{RETIRED_TOP_LEVEL_KEYS[key]}. The block is ignored; "
                    f"delete it once the extension is in place."
                )
            elif key not in KNOWN_TOP_LEVEL_KEYS:
                print(
                    f"[config] WARNING: unknown top-level key '{key}' in {path} "
                    f"— it is ignored (known keys: {', '.join(sorted(KNOWN_TOP_LEVEL_KEYS))})"
                )

        if "tasks_path" in data:
            config.tasks_path = data["tasks_path"]

        tasks_path = Path(config.tasks_path)
        if not tasks_path.is_absolute():
            config.tasks_path = str(path.resolve().parent / tasks_path)

        if "agents" in data:
            for name, settings in data["agents"].items():
                unconsumed = set(settings) - CONSUMED_AGENT_KEYS
                if unconsumed:
                    print(
                        f"[config] WARNING: agent '{name}' has settings nothing consumes: "
                        f"{', '.join(sorted(unconsumed))} "
                        f"(consumed keys: {', '.join(sorted(CONSUMED_AGENT_KEYS))})"
                    )
                config.agents[name] = AgentConfig(
                    model=settings.get("model", "gpt-4o-mini"),
                    settings={k: v for k, v in settings.items() if k != "model"},
                )
        
        if "llm" in data:
            llm = data["llm"] or {}
            _warn_unknown_nested("llm", llm, CONSUMED_LLM_KEYS, path)
            config.llm = LLMConfig(
                default_provider=llm.get("default_provider", "openai"),
                default_model=llm.get("default_model", "gpt-4o-mini"),
                api_key=llm.get("api_key"),
                base_url=llm.get("base_url"),
                zai_api_key=llm.get("zai_api_key"),
            )

        if "triage" in data:
            triage = data["triage"] or {}
            _warn_unknown_nested("triage", triage, CONSUMED_TRIAGE_KEYS, path)
            config.triage = TriageConfig(
                model=triage.get("model", TriageConfig.model),
                api_base=triage.get("api_base"),
                api_key=triage.get("api_key"),
            )

        if "notify" in data:
            notify = data["notify"] or {}
            _warn_unknown_nested("notify", notify, CONSUMED_NOTIFY_KEYS, path)
            config.notify = NotifyConfig(
                enabled=notify.get("enabled", True),
                url=notify.get("url", NotifyConfig.url),
                telegram_chat_id=(
                    str(notify["telegram_chat_id"])
                    if notify.get("telegram_chat_id") is not None
                    else None
                ),
                telegram_token_env=notify.get("telegram_token_env", NotifyConfig.telegram_token_env),
                cycle_summary=notify.get("cycle_summary", False),
            )

        if "hub" in data:
            hub = data["hub"] or {}
            _warn_unknown_nested("hub", hub, {"url", "actor", "secret_env", "timeout_s", "lease_ttl_s"}, path)
            config.hub = HubConfig(
                url=hub.get("url"), actor=hub.get("actor", "harness"),
                secret_env=hub.get("secret_env", "AGENTCO_HUB_SECRET"),
                timeout_s=int(hub.get("timeout_s", 30)), lease_ttl_s=int(hub.get("lease_ttl_s", 3600)),
            )

        if "egress" in data:
            egress = data["egress"] or {}
            _warn_unknown_nested("egress", egress, {"routes_path"}, path)
            config.egress = EgressConfig(routes_path=egress.get("routes_path"))

        if "humans" in data:
            humans = data["humans"] or {}
            _warn_unknown_nested("humans", humans, CONSUMED_HUMANS_KEYS, path)
            config.humans = HumansConfig(
                enabled=humans.get("enabled", True),
                escalate_to=humans.get("escalate_to"),
            )

        if "tiers" in data:
            tiers = data["tiers"] or {}
            # Same loud posture as the other nested blocks: a tier name the loader
            # does not know (e.g. the deferred `local`) is warned and dropped —
            # config that is parsed-but-inert is a lie the library tells its user.
            _warn_unknown_nested("tiers", tiers, KNOWN_TIER_KEYS, path)
            models = dict(DEFAULT_TIERS)
            for name, model in tiers.items():
                if name in KNOWN_TIER_KEYS:
                    models[name] = model
            config.tiers = TiersConfig(models=models)

        if "backoff" in data:
            backoff = data["backoff"] or {}
            _warn_unknown_nested("backoff", backoff, CONSUMED_BACKOFF_KEYS, path)
            config.backoff = BackoffConfig(
                enabled=backoff.get("enabled", True),
                base=str(backoff.get("base", BackoffConfig.base)),
                factor=backoff.get("factor", BackoffConfig.factor),
                max=str(backoff.get("max", BackoffConfig.max)),
            )
            # Parse-but-consume honesty: a malformed block is warned here at the
            # load boundary too, so the lie is loud even for callers that never
            # reach `agentco doctor`. The cycle gate degrades to disabled.
            errs = config.backoff.validation_errors()
            if errs:
                print(
                    f"[config] WARNING: backoff block in {path} is malformed: "
                    f"{'; '.join(errs)} — backoff will be treated as DISABLED "
                    f"(every wake runs at baseline). Fix it or run `agentco doctor`."
                )

        if "executor" in data:
            executor = data["executor"] or {}
            _warn_unknown_nested("executor", executor, CONSUMED_EXECUTOR_KEYS, path)
            idle = executor.get("idle_timeout_s", DEFAULT_IDLE_TIMEOUT_S)
            # bool is an int subclass; `idle_timeout_s: true` is a typo, not a
            # duration. Warn-and-default rather than fail: a fat-fingered value
            # must never take a cycle down, but it must never be silent either.
            if isinstance(idle, bool) or not isinstance(idle, int) or idle < 0:
                print(
                    f"[config] WARNING: executor.idle_timeout_s={idle!r} in {path} "
                    f"is not a non-negative integer (seconds) — falling back to "
                    f"{DEFAULT_IDLE_TIMEOUT_S}s (0 disables the watchdog)"
                )
                idle = DEFAULT_IDLE_TIMEOUT_S
            config.executor = ExecutorConfig(idle_timeout_s=idle)

        if "capabilities" in data:
            # Local import keeps config.py free of a module-level dependency on
            # beads.py, matching the `parse_duration` posture above. The
            # normalizer is shared with `Task.requires` on purpose: one
            # vocabulary, normalized identically on both sides of the match.
            from .beads import normalize_capabilities

            config.capabilities = normalize_capabilities(
                data["capabilities"],
                field_name="capabilities",
                strict=False,  # a malformed manifest must not kill a daemon
                where=str(path),
            )

        if "instance" in data:
            config.instance = data["instance"]

        return config

    def save(self, path: Path | str = "config.yaml") -> None:
        """Save config to YAML file."""
        data = {
            "tasks_path": self.tasks_path,
            "agents": {
                name: {
                    "model": agent.model,
                    **agent.settings,
                }
                for name, agent in self.agents.items()
            },
            "llm": {
                "default_provider": self.llm.default_provider,
                "default_model": self.llm.default_model,
            },
            "triage": {
                "model": self.triage.model,
            },
            "notify": {
                "enabled": self.notify.enabled,
                "url": self.notify.url,
            },
            "tiers": dict(self.tiers.models),
        }

        if self.llm.api_key:
            data["llm"]["api_key"] = self.llm.api_key
        if self.llm.base_url:
            data["llm"]["base_url"] = self.llm.base_url
        if self.llm.zai_api_key:
            data["llm"]["zai_api_key"] = self.llm.zai_api_key
        if self.triage.api_base:
            data["triage"]["api_base"] = self.triage.api_base
        if self.triage.api_key:
            data["triage"]["api_key"] = self.triage.api_key
        if self.notify.telegram_chat_id:
            data["notify"]["telegram_chat_id"] = self.notify.telegram_chat_id
            data["notify"]["telegram_token_env"] = self.notify.telegram_token_env
        if self.notify.cycle_summary:
            data["notify"]["cycle_summary"] = True
        # Persist humans only when non-default (disabled) so the kill-switch
        # survives a save/load round-trip. A default (enabled) instance stays
        # visually clean.
        if not self.humans.enabled or self.humans.escalate_to:
            data["humans"] = {"enabled": self.humans.enabled}
            if self.humans.escalate_to:
                data["humans"]["escalate_to"] = self.humans.escalate_to
        # Backoff is only serialized when it deviates from the defaults — an
        # instance that never touched it stays byte-identical to today's output.
        if self.backoff != BackoffConfig():
            data["backoff"] = {
                "enabled": self.backoff.enabled,
                "base": self.backoff.base,
                "factor": self.backoff.factor,
                "max": self.backoff.max,
            }
        # Same non-default-only posture as humans/backoff: a node that declares
        # no capabilities stays byte-identical to today's output, while a node
        # that does must round-trip it — a manifest lost in a save is a lane
        # that silently stops working after the next `agentco init`.
        if self.capabilities:
            data["capabilities"] = list(self.capabilities)
        if self.instance:
            data["instance"] = self.instance
        
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
