"""rebar root/config resolution (Python side).

Mirrors ``_engine/rebar-config.sh`` so the library and CLI agree with the bash
engine on repo-root and config-file location.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from rebar._config_schema import _ALIASES as _ALIASES
from rebar._config_schema import _RESERVED_SECTIONS as _RESERVED_SECTIONS
from rebar._config_schema import _SECTION_CLASSES as _SECTION_CLASSES

# The typed config SCHEMA (dataclasses + coercion + section tables) lives in the
# sibling module rebar._config_schema. Import the names config.py uses, then re-export
# every other moved name so `from rebar.config import X` keeps working (public API).
from rebar._config_schema import (
    _SECTIONS,
    Config,
    ConfigError,
    _as_bool,
    coerce_sparse,
    merge_sparse,
)
from rebar._config_schema import CompactConfig as CompactConfig
from rebar._config_schema import EnsureConfig as EnsureConfig
from rebar._config_schema import JiraConfig as JiraConfig
from rebar._config_schema import McpConfig as McpConfig
from rebar._config_schema import ReconcilerConfig as ReconcilerConfig
from rebar._config_schema import ScratchConfig as ScratchConfig
from rebar._config_schema import SyncConfig as SyncConfig
from rebar._config_schema import TicketClarityConfig as TicketClarityConfig
from rebar._config_schema import TicketConfig as TicketConfig
from rebar._config_schema import TrackerConfig as TrackerConfig
from rebar._config_schema import UiConfig as UiConfig
from rebar._config_schema import VerifyConfig as VerifyConfig
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


# Plan-review criteria authoring-guide deep-links (epic cite-stone-sea / WS10). A NARROW env-read
# — the base URL for the generated guide's per-criterion anchors — NOT a typed TOML config key
# (deep-links are a plan-review rendering concern, not core config surface).
def plan_review_docs_url(explicit_root: str | os.PathLike[str] | None = None) -> str:
    """Base URL for the plan-review criteria authoring guide (no trailing ``#anchor``):
    ``REBAR_DOCS_URL`` if set, else a repo-relative ``file://`` path to the generated
    ``docs/plan-review-criteria-guide.md``."""
    env = os.environ.get("REBAR_DOCS_URL", "").strip()
    if env:
        return env.rstrip("/")
    return (repo_root(explicit_root) / "docs" / "plan-review-criteria-guide.md").as_uri()


def plan_review_guide_anchor(
    criterion_id: str, explicit_root: str | os.PathLike[str] | None = None
) -> str:
    """A stable deep-link to a criterion's guide section: ``<base>#<criterion-id lower-cased>``
    (the anchor matches the guide's ``## <criterion-id>`` heading slug)."""
    return f"{plan_review_docs_url(explicit_root)}#{criterion_id.lower()}"


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
    (EV-3b)."""
    return os.environ.get("REBAR_TRACKER_DIR") or None


def tracker_dir(root: str | os.PathLike[str] | None = None) -> Path:
    """Path to the ticket event store, resolved through the full config precedence:
    the explicit env override (``REBAR_TRACKER_DIR``) wins verbatim; otherwise the
    configured ``tracker.dir``
    (``-c`` flag > project/user config > default ``.tickets-tracker``) — an absolute
    value relocates the store (EV-3b), a relative one is the dir name under the repo
    root. Previously this consulted env only; it now reads the typed config too."""
    env = tracker_dir_override()
    if env:
        return Path(env)
    try:
        name = load_config(root).tracker.dir
    except ConfigError:
        # Locating the store must not be coupled to config validity (it was env-only
        # before): a malformed config falls back to the default name. The fail-closed
        # gates (close/verify) surface the ConfigError via their own load_config.
        name = ".tickets-tracker"
    return Path(name) if os.path.isabs(name) else repo_root(root) / name


def tickets_branch(root: str | os.PathLike[str] | None = None) -> str:
    """The orphan git branch the ticket event log lives on (and the basis for its
    ``origin/<branch>`` ref), resolved through the full config precedence: the
    configured ``tracker.branch`` (``-c`` flag > ``REBAR_TRACKER_BRANCH`` env >
    project/user config > default ``tickets``). The single source of the branch name
    for every git path (init/sync/push/reconciler/fsck/reads).

    Unlike :func:`tracker_dir`, a malformed config is NOT swallowed here: silently
    defaulting the branch could mis-route writes to the wrong branch (a data-integrity
    risk), so the ``ConfigError`` propagates and the operation fails loudly."""
    return load_config(root).tracker.branch


def tickets_remote(root: str | os.PathLike[str] | None = None) -> str:
    """The git remote the ticket event-log branch syncs to — push, fetch/reconcile, the
    ``fsck`` PUSH_PENDING check, and the attested ticket-store materialization — resolved
    through the full config precedence (``-c`` flag > ``REBAR_SYNC_REMOTE`` env >
    project/user config > default ``origin``). The single source of the remote name for
    every ticket git-network path; the remote counterpart to :func:`tickets_branch`.

    Split-residency setups (code reviewed on a ``gerrit`` remote; the tickets branch's
    source of truth on a ``github``/``origin`` remote for a downstream sync) set this so
    the store no longer hard-assumes ``origin`` is the ticket remote. Like
    :func:`tickets_branch`, a malformed config is NOT swallowed here: silently defaulting
    could mis-route a push to the wrong remote, so the ``ConfigError`` propagates."""
    return load_config(root).sync.remote


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
        raw = _read_toml_table(path, pyproject=(kind == "pyproject"))
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


def _config_probe_paths(root: str | os.PathLike[str] | None = None) -> list[Path]:
    """Every project-config location the discovery walk PROBES (present or not),
    mirroring :func:`_discover_project_config`'s candidate order. Including their
    stat-tokens in the resolved-Config validation lets a warm cache hit detect a
    config file that APPEARS where none was found (or a higher-priority one
    appearing) — the gap that an only-read-files validation cannot catch (an empty
    validation is vacuously 'fresh' forever). This is exercised when ``load_config``
    runs BEFORE a config file is written in the same process (e.g. ``init`` →
    ``tracker_dir`` → resolve, then a config file is created). Stat-only, and only on
    a COLD resolve (cache miss), so it adds no warm-hit walk."""
    env = os.environ.get("REBAR_CONFIG")
    if env and Path(env).is_file():
        return [Path(env)]  # discovery short-circuits only when the env file EXISTS
    base = repo_root(root)
    # When REBAR_CONFIG points at a not-yet-existent file, discovery falls through to
    # the walk — so probe BOTH (the env path, to detect its creation, AND the walk).
    out: list[Path] = [Path(env)] if env else []
    cur = base
    while True:
        out.append(cur / "rebar.toml")
        out.append(cur / "pyproject.toml")
        if (cur / ".git").exists() or cur.parent == cur:
            break
        cur = cur.parent
    return out


def _resolve(
    root: str | os.PathLike[str] | None, cli_overrides: dict | None
) -> tuple[Config, tuple]:
    """Resolve the Config AND the validation token (stat-tokens of the files that
    fed the result PLUS the probed candidate locations), so a warm cache hit can
    detect both an edit to a read file and a config file APPEARING where none was
    found — without a re-walk."""
    layers, proj = _ordered_layers(root, cli_overrides=cli_overrides, strict=_strict_unknown_keys())
    cfg = Config.from_mapping(merge_sparse(*(sparse for _, sparse in layers)))
    up = user_config_path()
    read_paths: list[Path] = []
    if up.is_file():
        read_paths.append(up)
    if proj is not None:
        read_paths.append(proj[0])
    # Read files first, then the (possibly-absent) probe candidates — deduped, so a
    # newly-appearing higher-priority config invalidates the warm-hit cache.
    tokens: list[tuple] = []
    seen: set[str] = set()
    for p in [*read_paths, *_config_probe_paths(root), up]:
        key = str(p)
        if key not in seen:
            seen.add(key)
            tokens.append(_file_token(p))
    return cfg, tuple(tokens)


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


def mcp_readonly() -> bool:
    """THE shared resolver for the read-only gate (``mcp.readonly``). Resolves through
    the single-source typed config, so env ``REBAR_MCP_READONLY`` wins over the
    ``[tool.rebar.mcp] readonly`` file key (``load_config`` layers env above file), and
    fail-CLOSED to read-only on a malformed config (a broken config withholds writes
    rather than exposing them). Both read-only call sites route through this — the MCP
    server's write-tool gating (``mcp_server._readonly``) and the LLM runner's
    comment-tool withholding (``runner._readonly_gate``) — so the two cannot diverge
    (they once did: the runner read only the env var and ignored the file key)."""
    try:
        return load_config().mcp.readonly
    except ConfigError:
        return True


def read_config_file(path: str | os.PathLike[str]) -> Config:
    """Resolve a typed Config from a SINGLE explicit config file — no discovery, env,
    or user-layer merging. For callers that point at a specific file (e.g.
    ``clarity-check --config-file``); honors the same pyproject/TOML formats and
    coercion as the layered loader. Raises :class:`ConfigError` on an unreadable/
    invalid file (fail-closed)."""
    p = Path(path)
    if p.name == "pyproject.toml":
        raw = _read_toml_table(p, pyproject=True)
    else:
        raw = _read_toml_table(p, pyproject=False)
    return Config.from_mapping(raw, source=str(p), strict=_strict_unknown_keys())


def _emit_toml(data: dict) -> str:
    """Serialize a nested config mapping back to TOML text.

    A small, self-contained emitter covering the scalar value types a rebar config
    file legitimately holds — ``bool`` / ``int`` / ``float`` / ``str`` and a flat
    ``list`` of those — as top-level keys, then one ``[section]`` table per nested
    dict. It is deliberately NOT a general TOML writer (no inline tables, no nested
    tables, no datetimes): it exists only so :func:`write_jira_config` can round-trip
    a *rebar-owned* ``rebar.toml`` (read whole with stdlib ``tomllib`` → mutate the
    dict → re-emit), sidestepping any surgical text-splicing.

    **Fail-closed on an unsupported value type.** A full read-mutate-emit cycle would
    otherwise silently corrupt a value the emitter does not model (e.g. a datetime, a
    nested sub-table, or an array-of-tables). Rather than mis-emit, an unsupported
    type raises :class:`ConfigError` — the caller aborts WITHOUT writing, so an
    existing file is never clobbered. ``bool`` is checked before ``int`` (Python
    ``bool`` is an ``int`` subclass). Floats are emitted via ``repr`` so the value
    round-trips. Section/key order is preserved as given; empty tables are skipped;
    comments are not preserved (acceptable on a rebar-owned file — we never re-emit a
    user ``pyproject.toml``)."""

    def _scalar(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return repr(value)
        if isinstance(value, str):
            s = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{s}"'
        raise ConfigError(
            f"cannot serialize config value of type {type(value).__name__!r} "
            f"({value!r}); rebar's config writer only supports scalars and flat lists"
        )

    def _value(value: Any) -> str:
        if isinstance(value, list):
            return "[" + ", ".join(_scalar(v) for v in value) + "]"
        return _scalar(value)

    top = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    lines: list[str] = []
    for key, value in top.items():
        lines.append(f"{key} = {_value(value)}")
    for name, table in tables.items():
        if not table:  # never emit an empty [section] header
            continue
        if lines:
            lines.append("")
        lines.append(f"[{name}]")
        for key, value in table.items():
            if isinstance(value, dict):
                raise ConfigError(
                    f"cannot serialize nested sub-table [{name}.{key}]; rebar's config "
                    "writer supports only top-level keys and one level of [section]"
                )
            lines.append(f"{key} = {_value(value)}")
    return ("\n".join(lines) + "\n") if lines else ""


def write_jira_config(
    url: str = "",
    user: str = "",
    project: str = "",
    *,
    root: str | os.PathLike[str] | None = None,
    clear: bool = False,
) -> Path:
    """Persist the non-secret Jira settings (``url`` / ``user`` / ``project``) to a
    rebar-owned ``rebar.toml`` ``[jira]`` section and return the file written.

    The SECRET ``JIRA_API_TOKEN`` is NEVER a config key and is never written here —
    only the three connection coordinates are. This is the write counterpart to the
    read path in :func:`resolve_jira_settings` / :func:`load_config`.

    Target selection (deterministic): :func:`_discover_project_config` →

    * a ``rebar.toml`` → that file is the target.
    * a ``pyproject.toml`` / nothing → the target is ``<repo_root>/rebar.toml``
      (CREATED if absent). A user-owned ``pyproject.toml`` is NEVER edited; the fresh
      ``rebar.toml`` wins read precedence over pyproject (rebar.toml is probed first by
      the discovery walk).

    Mechanism: read the target whole with stdlib ``tomllib`` (so ``[jira]`` /
    ``jira = {…}`` inline-table / ``jira.url`` dotted-key forms all normalize to the
    same nested dict — there is no form-specific code and no way to append a
    duplicate section), mutate the in-memory ``jira`` table, and re-emit the whole
    file via :func:`_emit_toml`. No text-region splicing, so no section-end-boundary
    detection is needed. The write is atomic (temp file in the same directory +
    ``os.replace``); a single ``write`` cannot leave a torn/partial file. The
    read-modify-write is last-writer-wins across concurrent writers — fine for an
    interactive single-operator onboarding tool.

    With ``clear=True`` the three keys are removed (and an emptied ``jira`` table
    dropped) rather than set — the ``--reset`` path.

    Raises :class:`ConfigError` if an existing target is unreadable/malformed TOML
    (fail-closed: nothing is written) or the write itself fails."""
    base = repo_root(root)
    proj = _discover_project_config(root)
    if proj is not None and proj[1] == "toml":
        target = proj[0]
    else:
        target = base / "rebar.toml"

    data: dict[str, Any] = {}
    if target.is_file():
        try:
            data = _parse_toml(target)
        except ConfigError:
            raise  # malformed existing rebar.toml → fail closed, no write
    # tomllib returns a plain dict; ensure the jira table exists as a mutable dict.
    jira = data.get("jira")
    if not isinstance(jira, dict):
        jira = {}
    if clear:
        for k in ("url", "user", "project"):
            jira.pop(k, None)
    else:
        jira["url"] = url
        jira["user"] = user
        jira["project"] = project
    if jira:
        data["jira"] = jira
    else:
        data.pop("jira", None)

    text = _emit_toml(data)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        raise ConfigError(f"could not write config {target}: {exc}") from None
    return target


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
        table = _read_toml_table(path, pyproject=(kind == "pyproject"))
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
