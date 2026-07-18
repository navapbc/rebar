"""rebar raw-input config resolution -- file discovery, TOML parsing, env overrides.

Extracted from :mod:`rebar.config` (a pure structural split; no behavior change). This
is the raw-input resolution layer: repo-root and config-file location, the mtime-keyed
TOML parse cache, project/user config discovery, and the ``REBAR_<SECTION>_<KEY>``
env-override layer (including the legacy env aliases). The typed loader, precedence
merge, and the public ``load_config`` / ``tracker_dir`` surface stay in
:mod:`rebar.config`, which re-exports every name here so the public API is unchanged
(``from rebar.config import X`` still works).

This module is a LEAF: it imports only stdlib plus the sibling
:mod:`rebar._config_schema` and :mod:`rebar._deprecations` -- never :mod:`rebar.config`
-- so there is no import cycle. The logger is deliberately named ``"rebar.config"`` (not
this module) so existing log-capture tests for env-alias deprecation still match.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from rebar._config_schema import _SECTIONS, ConfigError, _as_bool
from rebar._deprecations import warn_deprecated

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
    """The explicit ``$REBAR_CONFIG`` file when set and present, else ``None``.
    (Project-config discovery — rebar.toml / a ``[tool.rebar]`` pyproject table —
    is done by :func:`_discover_project_config`.)"""
    env = os.environ.get("REBAR_CONFIG")
    if env and Path(env).is_file():
        return Path(env)
    return None


def tracker_dir_override() -> str | None:
    """The explicit ticket-store location override, or ``None`` when unset:
    ``REBAR_TRACKER_DIR``. The decoupled/relocated store is a supported feature
    (EV-3b).

    The removed ``TICKETS_TRACKER_DIR`` alias is a load-bearing tombstone checked
    HERE — the single env-read source, reached by ``tracker_dir()`` AND directly by
    ``rebar_reconciler/inbound_translate.py:_resolve_tracker_dir`` (both bypass
    ``load_config``). A ``load_config``-only check would leave those paths silently
    reading the wrong store."""
    if "TICKETS_TRACKER_DIR" in os.environ:
        from rebar._deprecations import RemovedInputError, removed_input

        raise RemovedInputError(removed_input("env", "TICKETS_TRACKER_DIR"))
    return os.environ.get("REBAR_TRACKER_DIR") or None


# Parsed-TOML cache keyed by (path, mtime_ns, size) so an edited file misses the
# cache and a stale parse is never served. ``rebar.config.reset_config_cache``
# imports this dict and mutates it in place (``.clear()``) -- it must never be
# rebound, so both modules share the one cache instance.
_TOML_CACHE: dict[tuple[str, int, int], dict] = {}


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
    ``[tool.rebar]`` table (stopping at ``.git`` / filesystem root). Returns
    ``(path, kind)`` or ``None`` where kind ∈ {toml, pyproject}."""
    env = os.environ.get("REBAR_CONFIG")
    if env and Path(env).is_file():
        p = Path(env)
        if p.name == "pyproject.toml":
            return (p, "pyproject")
        return (p, "toml")
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
    # ticket.default_assignee uses an ergonomic top-level env name (not the
    # auto-derived REBAR_TICKET_DEFAULT_ASSIGNEE) so a per-checkout/agent default is
    # easy to export (story c36c).
    ("ticket", "default_assignee"): "REBAR_DEFAULT_ASSIGNEE",
    # compact.COMPACTION_HORIZON_NS uses a clean top-level env name (not the
    # auto-derived, doubly-prefixed REBAR_COMPACT_COMPACTION_HORIZON_NS).
    ("compact", "COMPACTION_HORIZON_NS"): "REBAR_COMPACTION_HORIZON_NS",
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
# (truthy → "off"/disabled; falsy/unset → "on"/enabled, per the shared ``_as_bool``
# truthy convention); ``REBAR_ID_GUARD_MODE``
# is similarly value-mapped (warn → bypass/"true", raise/other → "false").
#
# ── Deprecation removal horizons ────────────────────────────────────────────────────
# The removal horizons (and permanent-vs-scheduled classification) for EVERY deprecated
# user-facing surface — including these env aliases — now live in the machine-readable
# registry in ``rebar._deprecations`` (the single source of truth), and every runtime
# signal routes through :func:`rebar._deprecations.warn_deprecated`. The DICT below is
# the RESOLUTION table (legacy env name -> section/key/canonical) the env layer consults.
# Every alias remaining here is now a PERMANENT ergonomic rename (REBAR_NO_SYNC,
# COMPACT_THRESHOLD, …); the once-scheduled aliases (REBAR_PUSH / TICKETS_TRACKER_DIR /
# REBAR_MCP_ALLOW_RECONCILE_LIVE) were removed pre-1.0 (DE7). Adding an alias here without
# a registry row fails the registry test.
_LEGACY_ENV_ALIASES: dict[str, tuple[str, str, str]] = {
    # legacy name                      -> (section, key, canonical name)
    "REBAR_NO_SYNC": ("sync", "pull", "REBAR_SYNC_PULL"),
    "COMPACT_THRESHOLD": ("compact", "threshold", "REBAR_COMPACT_THRESHOLD"),
    "SCRATCH_BASE_DIR": ("scratch", "base_dir", "REBAR_SCRATCH_BASE_DIR"),
    # reconciler.* (EV-3c renames) — canonical names are the ergonomic ones above.
    "REBAR_ACLI_TIMEOUT": ("reconciler", "jira_cli_timeout", "REBAR_JIRA_CLI_TIMEOUT"),
    # (reconciler.lock_max_retries + its env aliases REBAR_RECONCILER_LOCK_MAX_RETRIES /
    #  REBAR_RECONCILER_LOCK_RETRY_BUDGET were removed in epic dust-troth-naval / C4 —
    #  the b859 retry loop they tuned is superseded by the self-healing ref lock.)
    "RECONCILER_ABSENT_GET_BUDGET": (
        "reconciler",
        "deletion_probe_limit",
        "REBAR_RECONCILER_DELETION_PROBE_LIMIT",
    ),
    "REBAR_ID_GUARD_MODE": ("reconciler", "id_guard_bypass_unsafe", "REBAR_UNSAFE_ID_GUARD_BYPASS"),
}


def _map_legacy_env(legacy: str, value: str) -> str:
    """Map a legacy env value to its canonical config value. Non-identity cases:
    ``REBAR_NO_SYNC`` (negative→positive boolean flip; truthy per ``_as_bool`` →
    pull "off") and ``REBAR_ID_GUARD_MODE`` (the id-guard value-flip: ``warn`` →
    bypass/"true", ``raise``/other → "false")."""
    if legacy == "REBAR_NO_SYNC":
        # Honor the shared truthy convention (``_as_bool``: 1/true/yes/on, case- and
        # whitespace-insensitive) rather than "any non-empty, non-'0' string is set".
        # ``REBAR_NO_SYNC`` truthy → disable pull; falsy/unset → leave pull on.
        return "off" if _as_bool(value, legacy) else "on"
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
    for legacy, (sect, key, _canonical) in _LEGACY_ENV_ALIASES.items():
        if legacy in os.environ and key not in out.get(sect, {}):
            warn_deprecated(f"env:{legacy}", logger=logger)
            out.setdefault(sect, {})[key] = _map_legacy_env(legacy, os.environ[legacy])
    return out


def _strict_unknown_keys() -> bool:
    """Unknown-key policy for the legacy-config deprecation window. Default: WARN and
    ignore (``REBAR_CONFIG_UNKNOWN_KEYS`` unset / ``warn``) — a working install never
    breaks on an unknown/typo'd key during the window. Set
    ``REBAR_CONFIG_UNKNOWN_KEYS=error`` to hard-fail (the post-deprecation cutover, or
    an early opt-in to strict config). Any other value falls back to the safe WARN."""
    return os.environ.get("REBAR_CONFIG_UNKNOWN_KEYS", "").strip().lower() == "error"
