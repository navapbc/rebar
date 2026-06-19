"""rebar root/config resolution (Python side).

Mirrors ``_engine/rebar-config.sh`` so the library and CLI agree with the bash
engine on repo-root and config-file location.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("rebar.config")


def repo_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the repository root.

    Order: explicit arg > REBAR_ROOT > git toplevel of cwd.
    """
    if explicit is not None:
        return Path(explicit).resolve()
    env = os.environ.get("REBAR_ROOT")
    if env:
        return Path(env).resolve()
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if out:
            return Path(out).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.cwd()


def config_file(root: str | os.PathLike[str] | None = None) -> Path | None:
    """First existing of $REBAR_CONFIG, <root>/.rebar/config.conf, <root>/.rebar.conf."""
    env = os.environ.get("REBAR_CONFIG")
    if env and Path(env).is_file():
        return Path(env)
    base = repo_root(root)
    for candidate in (base / ".rebar" / "config.conf", base / ".rebar.conf"):
        if candidate.is_file():
            return candidate
    return None


# Warn-once registry for deprecated standalone env vars (those resolved outside the
# load_config cache, e.g. the tracker-dir override, which is read on a hot path).
_WARNED_LEGACY_ENV: set[str] = set()


def _warn_once_legacy_env(legacy: str, canonical: str) -> None:
    if legacy not in _WARNED_LEGACY_ENV:
        _WARNED_LEGACY_ENV.add(legacy)
        logger.warning("rebar config: env %s is deprecated; use %s", legacy, canonical)


def tracker_dir_override() -> str | None:
    """The explicit ticket-store location override, or ``None`` when unset:
    ``REBAR_TRACKER_DIR`` (canonical) or the deprecated ``TICKETS_TRACKER_DIR``
    (honored during the rename window with a one-time deprecation warning). The
    decoupled/relocated store is a supported feature (EV-3b)."""
    val = os.environ.get("REBAR_TRACKER_DIR")
    if val:
        return val
    legacy = os.environ.get("TICKETS_TRACKER_DIR")
    if legacy:
        _warn_once_legacy_env("TICKETS_TRACKER_DIR", "REBAR_TRACKER_DIR")
        return legacy
    return None


def tracker_dir(root: str | os.PathLike[str] | None = None) -> Path:
    """Path to the ticket event store (.tickets-tracker), honoring the env override
    (``REBAR_TRACKER_DIR``, deprecated alias ``TICKETS_TRACKER_DIR``)."""
    env = tracker_dir_override()
    if env:
        return Path(env)
    return repo_root(root) / ".tickets-tracker"


# ── Typed config (the single source of truth for non-secret settings) ─────────
#
# This is the in-memory schema the config-refinement work (epic a621) builds on:
# a stdlib dataclass (no pydantic-settings — core stays dependency-free) holding
# the CORE config keys. ``from_mapping`` parses a nested mapping (TOML
# ``[tool.rebar]`` shape) into the typed object — coercing types, applying
# defaults, honoring legacy aliases, and WARNING (never silently dropping) on
# unknown keys. The TOML loader + discovery + layering (CLI > env > project >
# user > defaults) and routing the existing reads through this are subsequent
# tasks; ``llm.*`` keys live in the optional ``rebar.llm`` layer (not here) so the
# stdlib core never depends on the agents extra. See docs/config.md.


class ConfigError(ValueError):
    """A config value is invalid. Raised at load time so problems fail fast at one
    site rather than surfacing deep in unrelated logic."""


_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off", ""}


def _src(source: str) -> str:
    return f" ({source})" if source else ""


def _as_bool(v: Any, key: str) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ConfigError(f"{key}: expected a boolean, got {v!r}")


def _as_int(v: Any, key: str, *, minimum: int | None = None) -> int:
    if isinstance(v, bool):  # bool is an int subclass — reject to catch e.g. true→1
        raise ConfigError(f"{key}: expected an integer, got boolean {v!r}")
    try:
        i = int(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{key}: expected an integer, got {v!r}") from None
    if minimum is not None and i < minimum:
        raise ConfigError(f"{key}: must be >= {minimum}, got {i}")
    return i


def _as_str(v: Any, key: str) -> str:
    if isinstance(v, (dict, list)):
        raise ConfigError(f"{key}: expected a string, got {type(v).__name__}")
    return str(v)


def _as_choice(v: Any, key: str, choices: set[str]) -> str:
    s = str(v).strip().lower()
    if s not in choices:
        raise ConfigError(f"{key}: expected one of {sorted(choices)}, got {v!r}")
    return s


def _warn_unknown(section: str, leftover: dict, source: str, *, strict: bool = False) -> None:
    """Handle keys left over after coercion (unknown to the schema). During the
    deprecation window (``strict=False``, the default) WARN and ignore them — a typo
    guard that never breaks a working install. Past the cutover (``strict=True``, via
    ``REBAR_CONFIG_UNKNOWN_KEYS=error``) raise so the unknown key is a hard error."""
    if not leftover:
        return
    if strict:
        keys = ", ".join(f"{section}.{k}" for k in leftover)
        raise ConfigError(
            f"rebar config{_src(source)}: unknown key(s) {keys} "
            "(REBAR_CONFIG_UNKNOWN_KEYS=error — remove them or fix the typo)"
        )
    for k in leftover:
        logger.warning(
            "rebar config%s: unknown key '%s.%s' ignored (typo? see docs/config.md)",
            _src(source),
            section,
            k,
        )


@dataclass
class VerifyConfig:
    require_signature_for_close: bool = False
    # Opt-in completion-verification close gate: when true, closing a work ticket runs the
    # LLM completion-verifier (rebar.llm.verify_completion) and blocks on FAIL / unavailable
    # LLM (fail-closed; --force-close bypasses without signing). On PASS the verdict is signed.
    # Default off. NOTE: this and require_signature_for_close are ALTERNATIVES — the completion
    # gate signs AFTER the close, while the signature gate requires a signature BEFORE it, so
    # enabling both deadlocks a non-force close. Enable one.
    require_completion_verification_for_close: bool = False


@dataclass
class TicketConfig:
    display_mode: str = "auto"


@dataclass
class TicketClarityConfig:
    threshold: int = 5  # clarity-check pass threshold (section name matches the
    # legacy flat key `ticket_clarity.threshold`, so it reads with no alias)


@dataclass
class CompactConfig:
    threshold: int = 10


@dataclass
class SyncConfig:
    push: str = "always"  # always | async | off
    pull: str = "on"  # on | off


@dataclass
class McpConfig:
    readonly: bool = False
    allow_llm: bool = False
    allow_jira_sync: bool = False


@dataclass
class ReconcilerConfig:
    jira_cli_timeout: int = 0
    lock_max_retries: int = 5
    deletion_probe_limit: int = 20
    id_guard_bypass_unsafe: bool = False


@dataclass
class JiraConfig:
    url: str = ""
    user: str = ""
    project: str = ""


@dataclass
class ScratchConfig:
    base_dir: str = ""


@dataclass
class Config:
    """The typed core configuration — defaults baked in; build with
    :meth:`from_mapping`. Secrets are NOT here (env/.env only)."""

    verify: VerifyConfig = field(default_factory=VerifyConfig)
    ticket: TicketConfig = field(default_factory=TicketConfig)
    ticket_clarity: TicketClarityConfig = field(default_factory=TicketClarityConfig)
    compact: CompactConfig = field(default_factory=CompactConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    reconciler: ReconcilerConfig = field(default_factory=ReconcilerConfig)
    jira: JiraConfig = field(default_factory=JiraConfig)
    scratch: ScratchConfig = field(default_factory=ScratchConfig)

    @classmethod
    def from_mapping(cls, raw: dict | None, *, source: str = "", strict: bool = False) -> Config:
        """Build a Config from a nested mapping (TOML ``[tool.rebar]`` shape): coerce
        + validate present values, apply defaults for the rest, honor legacy
        aliases, and WARN (never silently drop) on unknown sections/keys — or, with
        ``strict=True``, hard-error on them (the post-deprecation cutover). Raises
        :class:`ConfigError` on an invalid value (fail-closed at load)."""
        sparse = coerce_sparse(raw, source=source, strict=strict)
        return cls(**{sect: _SECTION_CLASSES[sect](**vals) for sect, vals in sparse.items()})


# ── schema: the single source of coercion truth (sparse parse + defaults) ─────
_SECTION_CLASSES: dict[str, type] = {
    "verify": VerifyConfig,
    "ticket": TicketConfig,
    "ticket_clarity": TicketClarityConfig,
    "compact": CompactConfig,
    "sync": SyncConfig,
    "mcp": McpConfig,
    "reconciler": ReconcilerConfig,
    "jira": JiraConfig,
    "scratch": ScratchConfig,
}

# section -> {key -> coercer(value, dotted_key) -> coerced value (raises ConfigError)}
_SECTIONS: dict[str, dict] = {
    "verify": {
        "require_signature_for_close": lambda v, k: _as_bool(v, k),
        "require_completion_verification_for_close": lambda v, k: _as_bool(v, k),
    },
    "ticket": {"display_mode": lambda v, k: _as_str(v, k) or "auto"},
    "ticket_clarity": {"threshold": lambda v, k: _as_int(v, k, minimum=1)},
    "compact": {"threshold": lambda v, k: _as_int(v, k, minimum=1)},
    "sync": {
        "push": lambda v, k: _as_choice(v, k, {"always", "async", "off"}),
        "pull": lambda v, k: _as_choice(v, k, {"on", "off"}),
    },
    "mcp": {
        "readonly": lambda v, k: _as_bool(v, k),
        "allow_llm": lambda v, k: _as_bool(v, k),
        "allow_jira_sync": lambda v, k: _as_bool(v, k),
    },
    "reconciler": {
        "jira_cli_timeout": lambda v, k: _as_int(v, k, minimum=0),
        "lock_max_retries": lambda v, k: _as_int(v, k, minimum=0),
        "deletion_probe_limit": lambda v, k: _as_int(v, k, minimum=1),
        "id_guard_bypass_unsafe": lambda v, k: _as_bool(v, k),
    },
    "jira": {
        "url": lambda v, k: _as_str(v, k),
        "user": lambda v, k: _as_str(v, k),
        "project": lambda v, k: _as_str(v, k),
    },
    "scratch": {"base_dir": lambda v, k: _as_str(v, k)},
}

# section -> {deprecated_key -> canonical_key}
_ALIASES: dict[str, dict[str, str]] = {
    "verify": {"require_verdict_for_close": "require_signature_for_close"},
}

# Config sections owned by an OPTIONAL layer rather than the stdlib core typed
# Config — currently ``llm`` (the ``nava-rebar[agents]`` extra; resolved by
# ``rebar.llm.LLMConfig.from_env`` so the stdlib core never imports the agents
# stack). They are RECOGNISED by the core parser — neither warned as unknown nor
# coerced into :class:`Config` — and read raw via :func:`read_reserved_section`.
_RESERVED_SECTIONS: frozenset[str] = frozenset({"llm"})


def coerce_sparse(raw: dict | None, *, source: str = "", strict: bool = False) -> dict:
    """Coerce+validate a nested mapping into a SPARSE nested dict of ONLY the keys
    actually present (NO defaults applied) — the per-layer building block for
    precedence merging. Resolves legacy aliases (the legacy key is accepted, with a
    deprecation warning, regardless of ``strict``); raises :class:`ConfigError` on an
    invalid value. Unknown sections/keys WARN by default and, with ``strict=True``,
    hard-error (the deprecation cutover). Defaults are applied ONCE, at the end, by
    :meth:`Config.from_mapping` — so a lower-priority layer's default can never
    override a higher layer's explicit value."""
    raw = dict(raw or {})
    out: dict[str, dict] = {}
    for sect, val in raw.items():
        if sect in _RESERVED_SECTIONS:
            continue  # owned by an optional layer (e.g. llm → rebar.llm); not a core key
        if sect not in _SECTIONS:
            if strict:
                raise ConfigError(
                    f"rebar config{_src(source)}: unknown section [{sect}] "
                    "(REBAR_CONFIG_UNKNOWN_KEYS=error)"
                )
            logger.warning("rebar config%s: unknown section [%s] ignored", _src(source), sect)
            continue
        if not isinstance(val, dict):
            raise ConfigError(f"[{sect}]: expected a table/section, got {type(val).__name__}")
        d = dict(val)
        for old, new in _ALIASES.get(sect, {}).items():
            if old in d:
                if new not in d:
                    logger.warning(
                        "rebar config%s: '%s.%s' is deprecated; use '%s.%s'",
                        _src(source),
                        sect,
                        old,
                        sect,
                        new,
                    )
                    d[new] = d.pop(old)
                else:
                    d.pop(old)  # canonical key wins
        coerced: dict = {}
        for key, coercer in _SECTIONS[sect].items():
            if key in d:
                coerced[key] = coercer(d.pop(key), f"{sect}.{key}")
        _warn_unknown(sect, d, source, strict=strict)
        if coerced:
            out[sect] = coerced
    return out


def merge_sparse(*layers: dict | None) -> dict:
    """Deep-merge sparse config layers in precedence order — LATER layers win,
    per key. Each layer is a sparse nested dict from :func:`coerce_sparse`."""
    out: dict[str, dict] = {}
    for layer in layers:
        for sect, vals in (layer or {}).items():
            out.setdefault(sect, {}).update(vals)
    return out


# ── config-file discovery + layered load ──────────────────────────────────────
#
# Config resolution is on the COMMAND HOT PATH (every CLI invocation + many library
# reads resolve config; the verify gate and ticket.display_mode go through
# load_config). Two caches keep it cheap and bounded WITHOUT risking staleness:
#
#  * _TOML_CACHE memoizes a parsed TOML file by (path, mtime_ns, size) — so the
#    upward discovery walk and the final read never parse the same pyproject twice
#    (the walk's [tool.rebar]-presence probe and the subsequent table read share one
#    parse), and a repeated load reuses the parse. mtime+size in the key means an
#    edited file misses the cache, so a stale parse can never be served.
#  * _RESULT_CACHE memoizes a whole resolved Config by (root, cwd-when-root-implicit,
#    env-signature, cli-signature) so repeated resolutions in one process skip the
#    discovery walk + merge. Each entry also stores stat-tokens of the files that
#    were actually read; a warm hit re-stats ONLY those known paths (cheap; not a
#    walk, not a re-parse) and re-resolves if any changed or vanished. So even in a
#    long-running host (the MCP server) an EDITED config file is picked up — the
#    fail-closed verify gate cannot be pinned to a stale value by an in-process edit.
#    Errors are NEVER cached (the gate re-evaluates fail-closed). The one thing a
#    warm hit does NOT detect is a brand-NEW higher-priority config file appearing
#    where none was discovered (that needs a fresh walk) — call reset_config_cache()
#    to force one; this matches the "discovered once per process" contract.
_TOML_CACHE: dict[tuple[str, int, int], dict] = {}
# value: (config, validation) where validation is a tuple of file stat-tokens.
_RESULT_CACHE: dict[tuple, tuple[Config, tuple]] = {}

# Process-wide CLI overrides (the highest-precedence ``cli`` layer). Set once by the
# ``rebar`` CLI from ``-c section.key=value`` flags (git -c style); None for the
# library/MCP unless a caller passes ``cli_overrides=`` explicitly. load_config /
# resolve_with_sources fall back to this when no explicit ``cli_overrides`` arg is
# given, so the documented CLI-wins precedence holds across every config consumer
# without threading the overrides through every call site.
#
# NOT an MCP-concurrency hazard (verified, story uneven-sake-cocoa): this module
# global is set ONLY from the CLI entrypoint (``rebar -c …`` → set_cli_overrides in
# rebar._cli) and is NEVER set by the MCP server, so under ``rebar-mcp`` it stays
# ``None`` for the whole process — there is no per-request mutation to race.
_CLI_OVERRIDES: dict | None = None


def set_cli_overrides(overrides: dict | None) -> None:
    """Install the process-wide ``cli`` override layer (or clear it with ``None``).
    Invalidates the resolved-Config cache so the next resolve reflects the change."""
    global _CLI_OVERRIDES
    _CLI_OVERRIDES = overrides
    _RESULT_CACHE.clear()


def parse_cli_overrides(pairs: list[str]) -> dict:
    """Parse ``section.key=value`` strings (the ``rebar -c`` flag) into a nested
    sparse mapping. Raises :class:`ConfigError` on a malformed pair (missing ``=``
    or a non-dotted key) so a typo'd override fails loudly rather than being dropped."""
    out: dict[str, dict] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ConfigError(f"--config override {pair!r}: expected SECTION.KEY=VALUE")
        dotted, _, value = pair.partition("=")
        dotted = dotted.strip()
        if "." not in dotted:
            raise ConfigError(
                f"--config override {pair!r}: key must be dotted SECTION.KEY (got {dotted!r})"
            )
        sect, key = dotted.split(".", 1)
        out.setdefault(sect.strip(), {})[key.strip()] = value
    return out


def reset_config_cache() -> None:
    """Clear the config resolution caches (parsed-TOML + resolved-Config) and the
    process-wide CLI overrides. For one-shot CLI processes this is never needed;
    tests call it between cases, and a long-running host may call it to force a
    re-read after editing config files."""
    global _CLI_OVERRIDES
    _TOML_CACHE.clear()
    _RESULT_CACHE.clear()
    _WARNED_LEGACY_ENV.clear()
    _CLI_OVERRIDES = None


def _parse_toml(path: Path) -> dict:
    """Parse a whole TOML file, memoized by (path, mtime, size). Raises
    :class:`ConfigError` if the file cannot be read or parsed — the single place
    parse errors turn into the fail-closed signal."""
    import tomllib

    try:
        st = path.stat()
    except OSError as exc:
        raise ConfigError(f"could not read config {path}: {exc}") from None
    cache_key = (str(path), st.st_mtime_ns, st.st_size)
    hit = _TOML_CACHE.get(cache_key)
    if hit is not None:
        return hit
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"could not read config {path}: {exc}") from None
    _TOML_CACHE[cache_key] = data
    return data


def _read_toml_table(path: Path, *, pyproject: bool) -> dict:
    """Read a TOML config: the ``[tool.rebar]`` table for a pyproject.toml, else the
    whole top-level table (standalone rebar.toml / user config.toml)."""
    data = _parse_toml(path)
    table = data.get("tool", {}).get("rebar", {}) if pyproject else data
    return table if isinstance(table, dict) else {}


# Legacy FLAT (non-dotted) ``.rebar/config.conf`` keys mapped to a typed section.key
# for back-compat. Values are transformed by :func:`_map_legacy_flat_conf`. The only
# entry is the id-guard mode, whose flat key predates the typed reconciler section.
_LEGACY_FLAT_CONF_KEYS: dict[str, tuple[str, str]] = {
    "rebar_id_guard_mode": ("reconciler", "id_guard_bypass_unsafe"),
}


def _map_legacy_flat_conf(key: str, value: str) -> str:
    """Map a legacy flat-conf value to its typed config value. The only case is
    ``rebar_id_guard_mode`` (the id-guard value-flip: ``warn`` → bypass/"true",
    ``raise``/other → "false")."""
    if key == "rebar_id_guard_mode":
        return "true" if value.strip().lower() == "warn" else "false"
    return value


def _read_legacy_conf(path: Path) -> dict:
    """Read the legacy flat ``.rebar/config.conf`` (dotted ``section.key=value``)
    into a nested sparse mapping (values stay strings; coerce_sparse types them).
    A handful of legacy NON-dotted keys (:data:`_LEGACY_FLAT_CONF_KEYS`, e.g.
    ``rebar_id_guard_mode``) are mapped to their typed section.key for back-compat,
    with the canonical dotted key winning if both appear in the file."""
    out: dict[str, dict] = {}
    flat_legacy: dict[tuple[str, str], str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read config {path}: {exc}") from None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if "." in k:
            sect, key = k.split(".", 1)
            out.setdefault(sect, {})[key] = v
        elif k in _LEGACY_FLAT_CONF_KEYS:
            flat_legacy[_LEGACY_FLAT_CONF_KEYS[k]] = _map_legacy_flat_conf(k, v)
    # Apply legacy flat keys only where the canonical dotted key was not set
    # (canonical wins); the legacy flat name stays honored during the back-compat window.
    for (sect, key), val in flat_legacy.items():
        out.setdefault(sect, {}).setdefault(key, val)
    return out


def _pyproject_rebar_state(path: Path) -> str:
    """Whether a ``pyproject.toml`` carries a ``[tool.rebar]`` table:
    ``"has"`` / ``"absent"`` / ``"unreadable"`` (won't parse). An unreadable
    pyproject is reported as such — NOT silently skipped — so a present-but-
    unparseable gate config can fail CLOSED rather than leaking the security gate.

    Parses via the shared mtime-keyed cache, so when a ``[tool.rebar]`` pyproject is
    selected the subsequent :func:`_read_toml_table` reuses this parse rather than
    re-reading the file (no double-parse on the hot path)."""
    try:
        data = _parse_toml(path)
    except ConfigError:
        return "unreadable"
    return "has" if isinstance(data.get("tool", {}).get("rebar"), dict) else "absent"


def _discover_project_config(root: str | os.PathLike[str] | None = None) -> tuple[Path, str] | None:
    """Find the project config: ``$REBAR_CONFIG`` (explicit) first; else walk UP
    from the repo root for the nearest ``rebar.toml`` or a ``pyproject.toml`` with a
    ``[tool.rebar]`` table (stopping at ``.git`` / filesystem root); else the legacy
    ``.rebar/config.conf`` / ``.rebar.conf``. Returns ``(path, kind)`` or ``None``
    where kind ∈ {toml, pyproject, legacy}."""
    env = os.environ.get("REBAR_CONFIG")
    if env and Path(env).is_file():
        p = Path(env)
        if p.name == "pyproject.toml":
            return (p, "pyproject")
        return (p, "toml" if p.suffix == ".toml" else "legacy")
    base = repo_root(root)
    cur = base
    while True:
        rt = cur / "rebar.toml"
        if rt.is_file():
            return (rt, "toml")
        pp = cur / "pyproject.toml"
        # A pyproject with [tool.rebar] is the config; an UNREADABLE pyproject is
        # also selected (so _read_toml_table raises ConfigError -> the verify gate
        # fails CLOSED) — never silently skip a present-but-unparseable gate config.
        # (rebar.toml above takes precedence, so this only bites when it's the
        # would-be-chosen config.) An "absent" (parses, no [tool.rebar]) pyproject
        # is skipped, and the walk continues.
        if pp.is_file() and _pyproject_rebar_state(pp) in ("has", "unreadable"):
            return (pp, "pyproject")
        if (cur / ".git").exists() or cur.parent == cur:
            break  # repo boundary / filesystem root
        cur = cur.parent
    for cand in (base / ".rebar" / "config.conf", base / ".rebar.conf"):
        if cand.is_file():
            return (cand, "legacy")
    return None


def user_config_path() -> Path:
    """User-level config path (hand-rolled XDG; no platformdirs):
    ``$XDG_CONFIG_HOME/rebar/config.toml``, default ``~/.config/rebar/config.toml``.

    ``~/.config`` is used on ALL platforms (incl. macOS — we deliberately do not use
    ``~/Library/Application Support``, matching ruff/black/mypy's predictable dev-tool
    convention). Per the XDG spec a non-absolute ``XDG_CONFIG_HOME`` is ignored (it
    would otherwise resolve relative to cwd — non-portable), falling back to the
    default."""
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if not base or not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "rebar" / "config.toml"


# Per-key CANONICAL env-var name, where the ergonomic/established name does NOT
# match the auto-derived ``REBAR_<SECTION>_<KEY>``. These are the NON-deprecated,
# no-warning overrides of the config-file key (the env layer). Keys absent here use
# the auto-derived name. This resolves the reconciler/jira "nice env name vs nested
# section key" mismatch (e.g. reconciler.jira_cli_timeout ← REBAR_JIRA_CLI_TIMEOUT,
# not REBAR_RECONCILER_JIRA_CLI_TIMEOUT) WITHOUT renaming the established env vars.
_CANONICAL_ENV_NAMES: dict[tuple[str, str], str] = {
    ("reconciler", "jira_cli_timeout"): "REBAR_JIRA_CLI_TIMEOUT",
    ("reconciler", "id_guard_bypass_unsafe"): "REBAR_UNSAFE_ID_GUARD_BYPASS",
    # jira.* keep the Atlassian-standard unprefixed env names (the secret
    # JIRA_API_TOKEN stays env-only and is NOT a config key).
    ("jira", "url"): "JIRA_URL",
    ("jira", "user"): "JIRA_USER",
    ("jira", "project"): "JIRA_PROJECT",
}


def _canonical_env_name(sect: str, key: str) -> str:
    """The canonical ``REBAR_<KEY>`` env override for a config key — the per-key
    override in :data:`_CANONICAL_ENV_NAMES` when present, else the auto-derived
    ``REBAR_<SECTION>_<KEY>``."""
    return _CANONICAL_ENV_NAMES.get((sect, key), f"REBAR_{sect.upper()}_{key.upper()}")


# Deprecated env vars that map to a canonical config key during the rename window
# (EV-1/EV-3/EV-3c). The OLD name still works — read only when the canonical
# counterpart is unset (canonical always wins) — with a deprecation warning.
# ``REBAR_NO_SYNC`` is a NEGATIVE boolean flipped to the positive ``sync.pull``
# (truthy → "off"/disabled; unset-or-"0" → "on"/enabled); ``REBAR_ID_GUARD_MODE``
# is similarly value-mapped (warn → bypass/"true", raise/other → "false").
_LEGACY_ENV_ALIASES: dict[str, tuple[str, str, str]] = {
    # legacy name                      -> (section, key, canonical name)
    "REBAR_PUSH": ("sync", "push", "REBAR_SYNC_PUSH"),
    "REBAR_NO_SYNC": ("sync", "pull", "REBAR_SYNC_PULL"),
    "COMPACT_THRESHOLD": ("compact", "threshold", "REBAR_COMPACT_THRESHOLD"),
    "SCRATCH_BASE_DIR": ("scratch", "base_dir", "REBAR_SCRATCH_BASE_DIR"),
    "REBAR_MCP_ALLOW_RECONCILE_LIVE": ("mcp", "allow_jira_sync", "REBAR_MCP_ALLOW_JIRA_SYNC"),
    # reconciler.* (EV-3c renames) — canonical names are the ergonomic ones above.
    "REBAR_ACLI_TIMEOUT": ("reconciler", "jira_cli_timeout", "REBAR_JIRA_CLI_TIMEOUT"),
    "REBAR_RECONCILER_LOCK_RETRY_BUDGET": (
        "reconciler",
        "lock_max_retries",
        "REBAR_RECONCILER_LOCK_MAX_RETRIES",
    ),
    "RECONCILER_ABSENT_GET_BUDGET": (
        "reconciler",
        "deletion_probe_limit",
        "REBAR_RECONCILER_DELETION_PROBE_LIMIT",
    ),
    "REBAR_ID_GUARD_MODE": ("reconciler", "id_guard_bypass_unsafe", "REBAR_UNSAFE_ID_GUARD_BYPASS"),
}


def _map_legacy_env(legacy: str, value: str) -> str:
    """Map a legacy env value to its canonical config value. Non-identity cases:
    ``REBAR_NO_SYNC`` (negative→positive boolean flip) and ``REBAR_ID_GUARD_MODE``
    (the id-guard value-flip: ``warn`` → bypass/"true", ``raise``/other → "false")."""
    if legacy == "REBAR_NO_SYNC":
        return "off" if (value and value != "0") else "on"
    if legacy == "REBAR_ID_GUARD_MODE":
        return "true" if value.strip().lower() == "warn" else "false"
    return value


def env_overrides() -> dict:
    """Sparse mapping of ``REBAR_<SECTION>_<KEY>`` env overrides (raw strings;
    coerce_sparse types them). Only the known config keys are read. Deprecated
    legacy env vars (:data:`_LEGACY_ENV_ALIASES`) are honored when their canonical
    counterpart is unset, with a deprecation warning."""
    out: dict[str, dict] = {}
    for sect, keys in _SECTIONS.items():
        for key in keys:
            name = _canonical_env_name(sect, key)
            if name in os.environ:
                out.setdefault(sect, {})[key] = os.environ[name]
    for legacy, (sect, key, canonical) in _LEGACY_ENV_ALIASES.items():
        if legacy in os.environ and key not in out.get(sect, {}):
            logger.warning("rebar config: env %s is deprecated; use %s", legacy, canonical)
            out.setdefault(sect, {})[key] = _map_legacy_env(legacy, os.environ[legacy])
    return out


# The precedence layers, lowest to highest. ``defaults`` is not a layer — it is
# applied once by Config.from_mapping after the sparse layers merge.
LAYER_ORDER: tuple[str, ...] = ("default", "user", "project", "env", "cli")


def _strict_unknown_keys() -> bool:
    """Unknown-key policy for the legacy-config deprecation window. Default: WARN and
    ignore (``REBAR_CONFIG_UNKNOWN_KEYS`` unset / ``warn``) — a working install never
    breaks on an unknown/typo'd key during the window. Set
    ``REBAR_CONFIG_UNKNOWN_KEYS=error`` to hard-fail (the post-deprecation cutover, or
    an early opt-in to strict config). Any other value falls back to the safe WARN."""
    return os.environ.get("REBAR_CONFIG_UNKNOWN_KEYS", "").strip().lower() == "error"


def _ordered_layers(
    root: str | os.PathLike[str] | None = None,
    *,
    cli_overrides: dict | None = None,
    strict: bool = False,
) -> tuple[list[tuple[str, dict]], tuple[Path, str] | None]:
    """Assemble the precedence layers, **lowest first**: user config < project
    config < ``REBAR_<KEY>`` env < CLI overrides. Each is a ``(label, sparse)``
    pair (``label`` ∈ :data:`LAYER_ORDER`); a layer absent on this machine is
    simply omitted. Also returns the discovered project config ``(path, kind)`` (or
    ``None``) for transparency reporting. Shared by :func:`load_config` and
    :func:`resolve_with_sources` so resolution and provenance never diverge."""
    layers: list[tuple[str, dict]] = []
    up = user_config_path()
    if up.is_file():
        layers.append(
            (
                "user",
                coerce_sparse(_read_toml_table(up, pyproject=False), source=str(up), strict=strict),
            )
        )
    proj = _discover_project_config(root)
    if proj is not None:
        path, kind = proj
        raw = (
            _read_legacy_conf(path)
            if kind == "legacy"
            else _read_toml_table(path, pyproject=(kind == "pyproject"))
        )
        layers.append(("project", coerce_sparse(raw, source=str(path), strict=strict)))
    layers.append(("env", coerce_sparse(env_overrides(), source="env", strict=strict)))
    if cli_overrides:
        layers.append(("cli", coerce_sparse(cli_overrides, source="cli", strict=strict)))
    return layers, proj


def _env_signature() -> tuple:
    """The config-relevant environment, as a hashable snapshot: the discovery/
    location pointers plus every ``REBAR_<SECTION>_<KEY>`` override. Two processes
    with the same snapshot (and same files) resolve identically — and it is the
    cache key's env component, so an env change misses the cache."""
    sig = [
        (name, os.environ.get(name))
        for name in (
            "REBAR_CONFIG",
            "XDG_CONFIG_HOME",
            "REBAR_ROOT",
            "REBAR_CONFIG_UNKNOWN_KEYS",  # strict/warn policy affects whether load raises
        )
    ]
    # Canonical env overrides (per-key nice names where they differ from the
    # auto-derived REBAR_<SECTION>_<KEY>).
    for sect, keys in _SECTIONS.items():
        for key in keys:
            n = _canonical_env_name(sect, key)
            sig.append((n, os.environ.get(n)))
    # Every deprecated alias (EV-1/EV-3/EV-3c) — a change to any flips the resolved
    # config, so each must miss the cache.
    for legacy in _LEGACY_ENV_ALIASES:
        sig.append((legacy, os.environ.get(legacy)))
    return tuple(sig)


def _cli_signature(cli_overrides: dict | None) -> tuple | None:
    """A hashable snapshot of CLI overrides (sorted nested items) for the cache key."""
    if not cli_overrides:
        return None
    return tuple(
        (sect, tuple(sorted(vals.items()))) for sect, vals in sorted(cli_overrides.items())
    )


def _file_token(path: Path) -> tuple[str, int | None, int | None]:
    """A cheap (path, mtime_ns, size) freshness token; ``(path, None, None)`` if the
    file is missing — so a deleted/created config flips the token and misses cache."""
    try:
        st = path.stat()
        return (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(path), None, None)


def _resolve(
    root: str | os.PathLike[str] | None, cli_overrides: dict | None
) -> tuple[Config, tuple]:
    """Resolve the Config AND the validation token (stat-tokens of the files that
    actually fed the result), so a warm cache hit can detect an edit without a walk."""
    layers, proj = _ordered_layers(root, cli_overrides=cli_overrides, strict=_strict_unknown_keys())
    cfg = Config.from_mapping(merge_sparse(*(sparse for _, sparse in layers)))
    read_paths: list[Path] = []
    up = user_config_path()
    if up.is_file():
        read_paths.append(up)
    if proj is not None:
        read_paths.append(proj[0])
    return cfg, tuple(_file_token(p) for p in read_paths)


def load_config(
    root: str | os.PathLike[str] | None = None, *, cli_overrides: dict | None = None
) -> Config:
    """Resolve the typed Config by layering, **highest precedence last**:
    defaults < user config < project config < ``REBAR_<KEY>`` env < CLI overrides.

    Each layer is coerced sparse, merged by precedence, then defaults applied ONCE
    — so a lower layer's default can never override a higher layer's explicit
    value, and the result is portable (no machine-specific state leaks in).

    Memoized per process (see the cache notes above): repeated resolutions on the
    command hot path skip the discovery walk + parse, but a warm hit re-stats the
    files it read and re-resolves if any changed (so an in-process config edit — incl.
    the verify gate — is honored). A :class:`ConfigError` is propagated and NOT cached
    (the gate re-evaluates fail-closed every call). See :func:`reset_config_cache`.

    ``cli_overrides`` defaults to the process-wide :data:`_CLI_OVERRIDES` (set by the
    ``rebar -c`` flag); pass an explicit dict to override it, or an explicit ``{}`` to
    deliberately opt OUT of the process global (no ``cli`` layer for this call)."""
    effective_cli = cli_overrides if cli_overrides is not None else _CLI_OVERRIDES
    key = (
        str(root) if root is not None else None,
        os.getcwd() if root is None else None,  # cwd resolves the root when implicit
        _env_signature(),
        _cli_signature(effective_cli),
    )
    entry = _RESULT_CACHE.get(key)
    if entry is not None:
        cfg, validation = entry
        if all(_file_token(Path(tok[0])) == tok for tok in validation):
            return cfg  # every file it read is unchanged → cache is fresh
    cfg, validation = _resolve(root, effective_cli)
    _RESULT_CACHE[key] = (cfg, validation)
    return cfg


def read_config_file(path: str | os.PathLike[str]) -> Config:
    """Resolve a typed Config from a SINGLE explicit config file — no discovery, env,
    or user-layer merging. For callers that point at a specific file (e.g.
    ``clarity-check --config-file``); honors the same pyproject/TOML/legacy formats and
    coercion as the layered loader. Raises :class:`ConfigError` on an unreadable/
    invalid file (fail-closed)."""
    p = Path(path)
    if p.name == "pyproject.toml":
        raw = _read_toml_table(p, pyproject=True)
    elif p.suffix == ".toml":
        raw = _read_toml_table(p, pyproject=False)
    else:
        raw = _read_legacy_conf(p)
    return Config.from_mapping(raw, source=str(p), strict=_strict_unknown_keys())


def read_reserved_section(name: str, root: str | os.PathLike[str] | None = None) -> dict:
    """Return the merged RAW sub-table for a :data:`_RESERVED_SECTIONS` section — one
    owned by an optional layer (e.g. ``llm`` → ``rebar.llm``), assembled from the SAME
    user-then-project file discovery as :func:`load_config` (project overrides user,
    per key) but WITHOUT core coercion: the owning layer applies its own typing and its
    own env/CLI overlay (see :func:`cli_overrides_for`). Values are raw TOML/conf types.

    Raises :class:`ConfigError` if a discovered config file is unreadable/malformed —
    the caller decides whether to fail or degrade (the agents layer degrades to
    env-only so a broken core config never breaks an LLM operation)."""
    merged: dict = {}
    up = user_config_path()
    if up.is_file():
        sub = _read_toml_table(up, pyproject=False).get(name)
        if isinstance(sub, dict):
            merged.update(sub)
    proj = _discover_project_config(root)
    if proj is not None:
        path, kind = proj
        table = (
            _read_legacy_conf(path)
            if kind == "legacy"
            else _read_toml_table(path, pyproject=(kind == "pyproject"))
        )
        sub = table.get(name)
        if isinstance(sub, dict):
            merged.update(sub)
    return merged


def cli_overrides_for(name: str) -> dict:
    """The process-wide ``rebar -c`` overrides for a single section (``{key: value}``,
    raw strings), or ``{}`` when none. Lets a reserved-section owner (e.g. ``rebar.llm``)
    honor ``rebar -c llm.KEY=VALUE`` as its highest-precedence layer without the key
    being part of the core typed Config."""
    sub = (_CLI_OVERRIDES or {}).get(name)
    return dict(sub) if isinstance(sub, dict) else {}


def resolve_with_sources(
    root: str | os.PathLike[str] | None = None, *, cli_overrides: dict | None = None
) -> tuple[Config, dict[str, dict[str, str]], tuple[Path, str] | None]:
    """Resolve the typed Config **and** record where each key's value came from.

    Returns ``(config, sources, project)`` where ``sources[section][key]`` is the
    winning layer label (``"default"`` when no layer set it, else ``"user"`` /
    ``"project"`` / ``"env"`` / ``"cli"``) and ``project`` is the discovered project
    config ``(path, kind)`` or ``None``. This is the data behind ``rebar config``
    (the precedence-transparency command). Resolution reuses the exact same layers
    as :func:`load_config`, so the reported provenance can never disagree with the
    value that load actually produced."""
    effective_cli = cli_overrides if cli_overrides is not None else _CLI_OVERRIDES
    layers, project = _ordered_layers(
        root, cli_overrides=effective_cli, strict=_strict_unknown_keys()
    )
    config = Config.from_mapping(merge_sparse(*(sparse for _, sparse in layers)))
    sources: dict[str, dict[str, str]] = {}
    for sect, keys in _SECTIONS.items():
        sources[sect] = {}
        for key in keys:
            label = "default"
            for layer_label, sparse in layers:  # lowest→highest: last match wins
                if key in sparse.get(sect, {}):
                    label = layer_label
            sources[sect][key] = label
    return config, sources, project
