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

    Order: explicit arg > REBAR_ROOT > PROJECT_ROOT > git toplevel of cwd.
    """
    if explicit is not None:
        return Path(explicit).resolve()
    env = os.environ.get("REBAR_ROOT") or os.environ.get("PROJECT_ROOT")
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


def tracker_dir(root: str | os.PathLike[str] | None = None) -> Path:
    """Path to the ticket event store (.tickets-tracker), honoring the env override."""
    env = os.environ.get("TICKETS_TRACKER_DIR")
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


def _warn_unknown(section: str, leftover: dict, source: str) -> None:
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


@dataclass
class TicketConfig:
    display_mode: str = "auto"


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
    compact: CompactConfig = field(default_factory=CompactConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    reconciler: ReconcilerConfig = field(default_factory=ReconcilerConfig)
    jira: JiraConfig = field(default_factory=JiraConfig)
    scratch: ScratchConfig = field(default_factory=ScratchConfig)

    @classmethod
    def from_mapping(cls, raw: dict | None, *, source: str = "") -> Config:
        """Build a Config from a nested mapping (TOML ``[tool.rebar]`` shape): coerce
        + validate present values, apply defaults for the rest, honor legacy
        aliases, and WARN (never silently drop) on unknown sections/keys. Raises
        :class:`ConfigError` on an invalid value (fail-closed at load)."""
        sparse = coerce_sparse(raw, source=source)
        return cls(**{sect: _SECTION_CLASSES[sect](**vals) for sect, vals in sparse.items()})


# ── schema: the single source of coercion truth (sparse parse + defaults) ─────
_SECTION_CLASSES: dict[str, type] = {
    "verify": VerifyConfig,
    "ticket": TicketConfig,
    "compact": CompactConfig,
    "sync": SyncConfig,
    "mcp": McpConfig,
    "reconciler": ReconcilerConfig,
    "jira": JiraConfig,
    "scratch": ScratchConfig,
}

# section -> {key -> coercer(value, dotted_key) -> coerced value (raises ConfigError)}
_SECTIONS: dict[str, dict] = {
    "verify": {"require_signature_for_close": lambda v, k: _as_bool(v, k)},
    "ticket": {"display_mode": lambda v, k: (_as_str(v, k) or "auto")},
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


def coerce_sparse(raw: dict | None, *, source: str = "") -> dict:
    """Coerce+validate a nested mapping into a SPARSE nested dict of ONLY the keys
    actually present (NO defaults applied) — the per-layer building block for
    precedence merging. Warns on unknown sections/keys; resolves legacy aliases;
    raises :class:`ConfigError` on an invalid value. Defaults are applied ONCE, at
    the end, by :meth:`Config.from_mapping` — so a lower-priority layer's default
    can never override a higher layer's explicit value."""
    raw = dict(raw or {})
    out: dict[str, dict] = {}
    for sect, val in raw.items():
        if sect not in _SECTIONS:
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
                        _src(source), sect, old, sect, new,
                    )
                    d[new] = d.pop(old)
                else:
                    d.pop(old)  # canonical key wins
        coerced: dict = {}
        for key, coercer in _SECTIONS[sect].items():
            if key in d:
                coerced[key] = coercer(d.pop(key), f"{sect}.{key}")
        _warn_unknown(sect, d, source)
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
def _read_toml_table(path: Path, *, pyproject: bool) -> dict:
    """Read a TOML config: the ``[tool.rebar]`` table for a pyproject.toml, else the
    whole top-level table (standalone rebar.toml / user config.toml)."""
    import tomllib

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"could not read config {path}: {exc}") from None
    table = data.get("tool", {}).get("rebar", {}) if pyproject else data
    return table if isinstance(table, dict) else {}


def _read_legacy_conf(path: Path) -> dict:
    """Read the legacy flat ``.rebar/config.conf`` (dotted ``section.key=value``)
    into a nested sparse mapping (values stay strings; coerce_sparse types them)."""
    out: dict[str, dict] = {}
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
    return out


def _pyproject_rebar_state(path: Path) -> str:
    """Whether a ``pyproject.toml`` carries a ``[tool.rebar]`` table:
    ``"has"`` / ``"absent"`` / ``"unreadable"`` (won't parse). An unreadable
    pyproject is reported as such — NOT silently skipped — so a present-but-
    unparseable gate config can fail CLOSED rather than leaking the security gate."""
    import tomllib

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
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
    ``$XDG_CONFIG_HOME/rebar/config.toml``, default ``~/.config/rebar/config.toml``."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "rebar" / "config.toml"


def env_overrides() -> dict:
    """Sparse mapping of ``REBAR_<SECTION>_<KEY>`` env overrides (raw strings;
    coerce_sparse types them). Only the known config keys are read."""
    out: dict[str, dict] = {}
    for sect, keys in _SECTIONS.items():
        for key in keys:
            name = f"REBAR_{sect.upper()}_{key.upper()}"
            if name in os.environ:
                out.setdefault(sect, {})[key] = os.environ[name]
    return out


# The precedence layers, lowest to highest. ``defaults`` is not a layer — it is
# applied once by Config.from_mapping after the sparse layers merge.
LAYER_ORDER: tuple[str, ...] = ("default", "user", "project", "env", "cli")


def _ordered_layers(
    root: str | os.PathLike[str] | None = None, *, cli_overrides: dict | None = None
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
        layers.append(("user", coerce_sparse(_read_toml_table(up, pyproject=False), source=str(up))))
    proj = _discover_project_config(root)
    if proj is not None:
        path, kind = proj
        raw = (
            _read_legacy_conf(path)
            if kind == "legacy"
            else _read_toml_table(path, pyproject=(kind == "pyproject"))
        )
        layers.append(("project", coerce_sparse(raw, source=str(path))))
    layers.append(("env", coerce_sparse(env_overrides(), source="env")))
    if cli_overrides:
        layers.append(("cli", coerce_sparse(cli_overrides, source="cli")))
    return layers, proj


def load_config(
    root: str | os.PathLike[str] | None = None, *, cli_overrides: dict | None = None
) -> Config:
    """Resolve the typed Config by layering, **highest precedence last**:
    defaults < user config < project config < ``REBAR_<KEY>`` env < CLI overrides.

    Each layer is coerced sparse, merged by precedence, then defaults applied ONCE
    — so a lower layer's default can never override a higher layer's explicit
    value, and the result is portable (no machine-specific state leaks in)."""
    layers, _ = _ordered_layers(root, cli_overrides=cli_overrides)
    return Config.from_mapping(merge_sparse(*(sparse for _, sparse in layers)))


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
    layers, project = _ordered_layers(root, cli_overrides=cli_overrides)
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
