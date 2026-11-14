"""Machine-level connections registry (spec: docs/connections.md).

One machine can run several harness instances, each of which must use ONLY the
model accounts and GitHub identity it was given. This module is the store those
instances name their connections from, plus the live probes that prove a
connection works before it is saved.

Design rules, all of which are tested:

* Secrets live ONLY in a separate 0600 file, never in the JSON, never in an
  exception message, never in `connect list`.
* The registry directory is 0700; a secret file looser than 0600 is refused.
* Probes are injectable (`runner=` / `opener=`) so tests never launch a real
  `claude`, `agy`, ssh or network call.
* `child_env` returns only the ADDITIONS for a subprocess; `strip_keys` names
  what the caller must REMOVE, because ambient credentials silently outrank
  the pinned ones.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

SCHEMA = "agentco.connection/1"
KINDS = ("claude_code", "agy", "openai_compat", "github")
AUTHS = ("subscription", "api_key")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
PROBE_PROMPT = "Reply with exactly: OK"


class ConnectionError_(Exception):
    """A registry rule was broken. Messages never contain secret material."""


ConnectionRegistryError = ConnectionError_


@dataclass(frozen=True)
class Connection:
    """One connection. Fields not relevant to a kind stay empty."""

    name: str
    kind: str
    auth: str = ""                  # claude_code: subscription | api_key
    config_dir: str = ""            # claude_code: its OWN CLAUDE_CONFIG_DIR
    secret_file: str = ""           # path to the 0600 secret, never the secret
    base_url: str = ""              # openai_compat
    model: str = ""                 # openai_compat
    ssh_key: str = ""               # github
    gitconfig: str = ""             # github
    gh_config_dir: str = ""         # github
    expect_github_user: str = ""    # github, optional
    validated_at: str = ""
    validated: str = ""


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    detail: str


# Probe seams: runner(argv, env, timeout) -> (returncode, stdout, stderr);
# opener(request, timeout) -> context-managed response with .read().
Runner = Callable[[list, dict, int], tuple]
Opener = Callable[..., object]


# ---- registry location + files ----------------------------------------------

def connections_dir() -> Path:
    env = os.environ.get("AGENTCO_CONNECTIONS_DIR", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".config" / "agentco" / "connections"


def _ensure_dir() -> Path:
    d = connections_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(d, 0o700)
    return d


def _json_path(name: str) -> Path:
    return connections_dir() / f"{name}.json"


def _check_name(name: str) -> None:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ConnectionError_(f"invalid connection name {name!r}: must match {NAME_RE.pattern}")


def _write_private(path: Path, text: str) -> None:
    """Atomic write that is 0600 from the first byte, so there is no window."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _expand(p: str) -> str:
    return os.path.expanduser(p) if p else p


def _check_secret_mode(path: str) -> None:
    p = Path(_expand(path))
    if not p.exists():
        return
    loose = stat.S_IMODE(p.stat().st_mode) & 0o077
    if loose:
        raise ConnectionError_(
            f"secret file {p} has mode {stat.S_IMODE(p.stat().st_mode):04o}; it must be 0600 "
            f"(fix with: chmod 600 {p})"
        )


def _validate_shape(conn: Connection) -> None:
    _check_name(conn.name)
    if conn.kind not in KINDS:
        raise ConnectionError_(f"unknown kind {conn.kind!r}; expected one of {', '.join(KINDS)}")
    if conn.kind == "claude_code":
        if conn.auth not in AUTHS:
            raise ConnectionError_(f"claude_code needs auth to be one of {', '.join(AUTHS)}")
        if not conn.config_dir:
            raise ConnectionError_("claude_code needs its own config_dir")
    if conn.kind == "openai_compat" and not (conn.base_url and conn.model):
        raise ConnectionError_("openai_compat needs base_url and model")
    if conn.kind == "github" and not conn.ssh_key:
        raise ConnectionError_("github needs ssh_key")


def _check_unique_agy(conn: Connection) -> None:
    if conn.kind != "agy":
        return
    for other in list_all():
        if other.kind == "agy" and other.name != conn.name:
            raise ConnectionError_(
                f"an agy connection ({other.name!r}) already exists; agy has a single login per "
                f"machine, so at most one is allowed"
            )


def precheck(conn: Connection, secret: Optional[str]) -> None:
    """Every rule `save` enforces, without writing anything."""
    _validate_shape(conn)
    _check_unique_agy(conn)
    if conn.kind == "claude_code" and not secret:
        existing = Path(_expand(conn.secret_file)) if conn.secret_file else _secret_path(conn.name)
        if not existing.exists():
            raise ConnectionError_("claude_code needs a secret (token or API key)")


def _secret_path(name: str) -> Path:
    return connections_dir() / f"{name}.secret"


# ---- public API --------------------------------------------------------------

def save(conn: Connection, secret: Optional[str] = None) -> Connection:
    """Persist a connection. `secret=None` keeps any existing secret file.

    Returns the saved Connection (with `secret_file` filled in when a secret
    was stored)."""
    precheck(conn, secret)
    d = _ensure_dir()
    if secret:
        sf = conn.secret_file or str(_secret_path(conn.name))
        conn = replace(conn, secret_file=sf)
        path = Path(_expand(sf))
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_private(path, secret.strip() + "\n")
    elif not conn.secret_file and _secret_path(conn.name).exists():
        conn = replace(conn, secret_file=str(_secret_path(conn.name)))
    data = {"schema": SCHEMA, **{k: v for k, v in asdict(conn).items() if v != ""}}
    _write_private(d / f"{conn.name}.json", json.dumps(data, indent=2) + "\n")
    return conn


def load(name: str) -> Connection:
    _check_name(name)
    p = _json_path(name)
    if not p.exists():
        raise ConnectionError_(f"connection {name!r} not found in {connections_dir()}")
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise ConnectionError_(f"connection file {p} is not valid JSON ({e.msg})") from None
    if data.get("schema") != SCHEMA:
        raise ConnectionError_(f"connection {name!r} has unsupported schema {data.get('schema')!r}")
    known = {f.name for f in fields(Connection)}
    conn = Connection(**{k: v for k, v in data.items() if k in known})
    if conn.secret_file:
        _check_secret_mode(conn.secret_file)
    return conn


def list_all() -> list:
    d = connections_dir()
    if not d.is_dir():
        return []
    return [load(p.stem) for p in sorted(d.glob("*.json")) if NAME_RE.match(p.stem)]


def remove(name: str) -> None:
    conn = load_unchecked(name)
    _json_path(name).unlink()
    if conn.secret_file:
        Path(_expand(conn.secret_file)).unlink(missing_ok=True)


def load_unchecked(name: str) -> Connection:
    """Like `load` but tolerates a loose secret mode, so `remove` can clean up."""
    try:
        return load(name)
    except ConnectionError_ as e:
        if "must be 0600" not in str(e):
            raise
        data = json.loads(_json_path(name).read_text())
        known = {f.name for f in fields(Connection)}
        return Connection(**{k: v for k, v in data.items() if k in known})


def read_secret(conn: Connection) -> Optional[str]:
    if not conn.secret_file:
        return None
    _check_secret_mode(conn.secret_file)
    p = Path(_expand(conn.secret_file))
    if not p.exists():
        raise ConnectionError_(f"secret file for {conn.name!r} is missing ({p})")
    return p.read_text().strip() or None


def _secret(conn: Connection, secret: Optional[str]) -> Optional[str]:
    return secret if secret is not None else read_secret(conn)


def child_env(conn: Connection, secret: Optional[str] = None) -> dict:
    """Env ADDITIONS for a subprocess using `conn`. Pair with `strip_keys`."""
    if conn.kind == "claude_code":
        tok = _secret(conn, secret)
        if not tok:
            raise ConnectionError_(f"{conn.name!r} has no readable secret")
        var = "CLAUDE_CODE_OAUTH_TOKEN" if conn.auth == "subscription" else "ANTHROPIC_API_KEY"
        return {"CLAUDE_CONFIG_DIR": _expand(conn.config_dir), var: tok}
    if conn.kind == "github":
        env = {"GIT_SSH_COMMAND": "ssh -i %s -o IdentitiesOnly=yes -o IdentityAgent=none"
                                  % shlex.quote(_expand(conn.ssh_key))}
        if conn.gitconfig:
            env["GIT_CONFIG_GLOBAL"] = _expand(conn.gitconfig)
        if conn.gh_config_dir:
            env["GH_CONFIG_DIR"] = _expand(conn.gh_config_dir)
        return env
    return {}


def strip_keys(conn: Connection) -> set:
    """Variables the caller MUST remove from the child env: ambient credentials
    that would silently win over (or leak past) the pinned connection."""
    if conn.kind == "claude_code":
        if conn.auth == "subscription":
            return {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}
        return {"CLAUDE_CODE_OAUTH_TOKEN"}
    if conn.kind == "github":
        return {"SSH_AUTH_SOCK"}
    return set()


def probe_env(conn: Connection, secret: Optional[str] = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in strip_keys(conn)}
    env.update(child_env(conn, secret))
    return env


# ---- openai_compat -> completion.Provider -----------------------------------

def _key_env_name(conn: Connection) -> str:
    return "AGENTCO_CONN_" + re.sub(r"[^A-Z0-9]", "_", conn.name.upper()) + "_KEY"


def to_provider(conn: Connection):
    """A `completion.Provider` for an openai_compat connection.

    The Provider carries only the NAME of an env var for the key
    (`api_key_env`); the wiring caller puts the value there for the call with
    `provider_env(conn)`, so no secret lives in the Provider object."""
    from .completion import Provider

    if conn.kind != "openai_compat":
        raise ConnectionError_(f"{conn.name!r} is {conn.kind}, not openai_compat")
    keyed = bool(conn.secret_file)
    return Provider(
        name=conn.name, base_url=conn.base_url, model=conn.model,
        api_key_env=_key_env_name(conn) if keyed else None, requires_key=keyed,
    )


def provider_env(conn: Connection) -> dict:
    """`{api_key_env: key}` to overlay on os.environ around `completion.complete`."""
    if conn.kind != "openai_compat" or not conn.secret_file:
        return {}
    key = read_secret(conn)
    return {_key_env_name(conn): key} if key else {}


# ---- validation (live probes) ------------------------------------------------

def _default_runner(argv, env, timeout):
    p = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _redact(text: str, secret: Optional[str]) -> str:
    text = (text or "").strip()
    if secret:
        text = text.replace(secret, "***")
    return text[:300]


def validate(conn: Connection, *, runner: Optional[Runner] = None, opener: Optional[Opener] = None,
             secret: Optional[str] = None, check: str = "chat") -> ValidationResult:
    """Run a LIVE probe. Never raises on a probe failure, and never echoes a secret."""
    runner = runner or _default_runner
    opener = opener or urllib.request.urlopen
    try:
        sec = _secret(conn, secret) if conn.secret_file or secret else None
        if conn.kind == "claude_code":
            return _probe_cli(["claude", "--print", PROBE_PROMPT, "--max-turns", "1"],
                              probe_env(conn, sec), runner, sec, 120)
        if conn.kind == "agy":
            return _probe_cli(["agy", "--print", PROBE_PROMPT, "--dangerously-skip-permissions",
                               "--print-timeout", "60s"], dict(os.environ), runner, None, 90)
        if conn.kind == "openai_compat":
            return _probe_openai(conn, sec, opener, check)
        if conn.kind == "github":
            return _probe_github(conn, runner)
    except ConnectionError_ as e:
        return ValidationResult(False, f"failed: {e}")
    return ValidationResult(False, f"failed: unknown kind {conn.kind!r}")


def _probe_cli(argv, env, runner, secret, timeout) -> ValidationResult:
    import time
    t0 = time.monotonic()
    try:
        rc, out, err = runner(argv, env, timeout)
    except FileNotFoundError:
        return ValidationResult(False, f"failed: {argv[0]} not found on PATH")
    except subprocess.TimeoutExpired:
        return ValidationResult(False, f"failed: {argv[0]} timed out after {timeout}s")
    if rc == 0 and "OK" in (out or ""):
        return ValidationResult(True, f"ok: {argv[0]} replied in {time.monotonic() - t0:.1f}s")
    return ValidationResult(False, f"failed: {argv[0]} exit {rc}: {_redact(err or out, secret)}")


def _probe_openai(conn, secret, opener, check) -> ValidationResult:
    base = conn.base_url.rstrip("/")
    headers = {"content-type": "application/json"}
    if secret:
        headers["authorization"] = f"Bearer {secret}"
    if check == "models":
        req = urllib.request.Request(base + "/models", headers=headers, method="GET")
    else:
        body = json.dumps({"model": conn.model, "messages": [{"role": "user", "content": PROBE_PROMPT}],
                           "max_tokens": 5, "stream": False}).encode()
        req = urllib.request.Request(base + "/chat/completions", data=body, headers=headers, method="POST")
    try:
        with opener(req, timeout=60) as resp:
            payload = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return ValidationResult(False, f"failed: {conn.base_url} returned HTTP {e.code}")
    except urllib.error.URLError as e:
        return ValidationResult(False, f"failed: {conn.base_url} unreachable ({e.reason})")
    except (TimeoutError, OSError, ValueError) as e:
        return ValidationResult(False, f"failed: {type(e).__name__}")
    if check == "models":
        return ValidationResult(True, "ok: /models reachable")
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if isinstance(choices, list) and choices:
        return ValidationResult(True, "ok: endpoint answered a chat completion")
    return ValidationResult(False, "failed: endpoint returned no completion")


def _probe_github(conn, runner) -> ValidationResult:
    argv = ["ssh", "-i", _expand(conn.ssh_key), "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "BatchMode=yes", "-T", "git@github.com"]  # leakguard: allow (public ssh login, not a person)
    env = {k: v for k, v in os.environ.items() if k not in strip_keys(conn)}
    try:
        rc, out, err = runner(argv, env, 30)
    except FileNotFoundError:
        return ValidationResult(False, "failed: ssh not found on PATH")
    except subprocess.TimeoutExpired:
        return ValidationResult(False, "failed: ssh timed out")
    m = re.search(r"Hi ([^!\s]+)!", (out or "") + (err or ""))
    if rc == 1 and m:
        user = m.group(1)
        if conn.expect_github_user and user != conn.expect_github_user:
            return ValidationResult(False, f"failed: authenticated as {user!r}, expected "
                                           f"{conn.expect_github_user!r}")
        return ValidationResult(True, f"ok: authenticated as {user}")
    return ValidationResult(False, f"failed: ssh exit {rc}: {_redact(err or out, None)}")


def with_validation(conn: Connection, result: ValidationResult) -> Connection:
    when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    detail = result.detail if result.ok else (
        result.detail if result.detail.startswith("failed") else f"failed: {result.detail}")
    return replace(conn, validated_at=when, validated=detail)


# ---- CLI: `agentic-co connect` ----------------------------------------------

def _status(conn: Connection) -> str:
    if not conn.validated:
        return "unvalidated"
    return conn.validated.split(":", 1)[0]


def build_connect_group():
    import click

    @click.group("connect", invoke_without_command=True)
    @click.pass_context
    def connect(ctx):
        """Machine-level connections: model accounts and git identities.

        With no subcommand, runs an interactive selector. Everything is also
        scriptable: `connect add <kind> --name ... [--secret-stdin]`.
        """
        if ctx.invoked_subcommand is None:
            _interactive(ctx)

    def _fail(e):
        raise click.ClickException(str(e))

    def _finish(conn, secret, no_validate, force=False):
        try:
            if not force and _json_path(conn.name).exists():
                raise ConnectionError_(f"connection {conn.name!r} already exists (use --force to replace)")
            precheck(conn, secret)
        except ConnectionError_ as e:
            _fail(e)
        if no_validate:
            conn = replace(conn, validated="unvalidated (--no-validate)")
        else:
            res = validate(conn, secret=secret)
            if not res.ok:
                raise click.ClickException(f"{res.detail}\nNot saved. Use --no-validate to save anyway.")
            conn = with_validation(conn, res)
        try:
            saved = save(conn, secret)
        except ConnectionError_ as e:
            _fail(e)
        click.echo(f"Saved {saved.name} ({saved.kind}): {saved.validated or 'unvalidated'}")

    def _interactive(ctx):
        click.echo("Connection kinds:")
        for i, k in enumerate(KINDS, 1):
            click.echo(f"  {i}. {k}")
        n = click.prompt("Kind", type=click.IntRange(1, len(KINDS)))
        kind = KINDS[n - 1]
        name = click.prompt("Name")
        kw = {}
        secret = None
        if kind == "claude_code":
            kw["auth"] = click.prompt("Auth", type=click.Choice(AUTHS), default="subscription")
            kw["config_dir"] = click.prompt("Config dir (its own CLAUDE_CONFIG_DIR)")
            secret = click.prompt("Token from `claude setup-token`" if kw["auth"] == "subscription"
                                  else "API key", hide_input=True)
        elif kind == "openai_compat":
            kw["base_url"] = click.prompt("Base URL")
            kw["model"] = click.prompt("Model")
            secret = click.prompt("API key (empty for none)", hide_input=True, default="",
                                  show_default=False) or None
        elif kind == "github":
            kw["ssh_key"] = click.prompt("SSH key path")
            kw["gitconfig"] = click.prompt("Gitconfig path", default="", show_default=False)
            kw["gh_config_dir"] = click.prompt("gh config dir", default="", show_default=False)
            kw["expect_github_user"] = click.prompt("Expected GitHub user (optional)", default="",
                                                    show_default=False)
        _finish(Connection(name=name, kind=kind, **kw), secret, False)

    def _read_secret_opt(secret_stdin, secret_from):
        if secret_from:
            return Path(secret_from).expanduser().read_text().strip() or None
        if secret_stdin:
            return sys.stdin.readline().strip() or None
        return None

    @connect.command("add")
    @click.argument("kind", type=click.Choice(KINDS))
    @click.option("--name", required=True)
    @click.option("--auth", type=click.Choice(AUTHS), default=None)
    @click.option("--config-dir", default="")
    @click.option("--base-url", default="")
    @click.option("--model", default="")
    @click.option("--ssh-key", default="")
    @click.option("--gitconfig", default="")
    @click.option("--gh-config-dir", default="")
    @click.option("--expect-github-user", default="")
    @click.option("--secret-stdin", is_flag=True, help="Read the secret from one line of stdin.")
    @click.option("--secret-from", default="", help="Read the secret from this file.")
    @click.option("--no-validate", is_flag=True, help="Save without a live probe; marked unvalidated.")
    @click.option("--force", is_flag=True, help="Replace an existing connection of the same name.")
    def add(kind, name, auth, config_dir, base_url, model, ssh_key, gitconfig, gh_config_dir,
            expect_github_user, secret_stdin, secret_from, no_validate, force):
        """Add a connection non-interactively."""
        secret = _read_secret_opt(secret_stdin, secret_from)
        conn = Connection(name=name, kind=kind, auth=auth or ("subscription" if kind == "claude_code" else ""),
                          config_dir=config_dir, base_url=base_url, model=model, ssh_key=ssh_key,
                          gitconfig=gitconfig, gh_config_dir=gh_config_dir,
                          expect_github_user=expect_github_user)
        _finish(conn, secret, no_validate, force)

    @connect.command("list")
    @click.option("--json", "as_json", is_flag=True)
    def list_cmd(as_json):
        """List connections. Never prints secrets."""
        try:
            conns = list_all()
        except ConnectionError_ as e:
            _fail(e)
        if as_json:
            click.echo(json.dumps([{k: v for k, v in asdict(x).items() if v != ""} for x in conns], indent=2))
            return
        if not conns:
            click.echo("No connections. Run `agentic-co connect` to add one.")
            return
        for x in conns:
            click.echo(f"{x.name:<24} {x.kind:<14} {x.auth or '-':<13} "
                       f"{x.validated_at or '-':<21} {_status(x)}")

    @connect.command("test")
    @click.argument("name")
    def test_cmd(name):
        """Re-run the live validation and record the result."""
        try:
            conn = load(name)
        except ConnectionError_ as e:
            _fail(e)
        res = validate(conn)
        try:
            save(with_validation(conn, res), None)
        except ConnectionError_ as e:
            _fail(e)
        click.echo(f"{name}: {res.detail}")
        if not res.ok:
            raise SystemExit(1)

    @connect.command("remove")
    @click.argument("name")
    @click.option("--yes", is_flag=True, help="Skip the confirmation.")
    def remove_cmd(name, yes):
        """Delete a connection's JSON and secret file."""
        try:
            load_unchecked(name)
        except ConnectionError_ as e:
            _fail(e)
        if not yes:
            click.confirm(f"Delete connection {name!r} and its secret file?", abort=True)
        try:
            remove(name)
        except ConnectionError_ as e:
            _fail(e)
        click.echo(f"Removed {name}.")

    return connect
