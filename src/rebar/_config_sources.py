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
from collections.abc import Mapping
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


def _deep_merge(base: dict, overlay: dict) -> dict:
    """``overlay`` layered over ``base``, RECURSIVELY through nested tables.

    A key holding a table in BOTH is merged key-by-key, so overriding one leaf never
    discards its siblings; anything else (scalar, array, or a type change) is REPLACED
    wholesale. Arrays are deliberately never appended: an appending merge would leave a
    project's ordered chain reachable behind an environment's, so the environment could
    never actually narrow one, and ``x = []`` could not mean "none".

    Deliberately NOT folded into :func:`rebar.config.read_reserved_section`, whose
    user-then-project merge is SHALLOW and shared by every reserved section — deepening
    it there would silently change layering for unrelated consumers."""
    out = dict(base)
    for key, value in overlay.items():
        prev = out.get(key)
        out[key] = (
            _deep_merge(prev, value)
            if isinstance(prev, dict) and isinstance(value, dict)
            else value
        )
    return out


def llm_config_file_pointer() -> Path | None:
    """The ``REBAR_LLM_CONFIG_FILE`` pointer as a path, or ``None`` when unset/blank.

    Raises :class:`ConfigError` naming the variable when it points at something that is
    not a readable file. Fail-loud is the point: an environment that asked for a specific
    file and silently got the checkout's would send the operator debugging the wrong config.

    The read below is a string LITERAL ``os.environ.get("REBAR_LLM_CONFIG_FILE")`` on
    purpose. ``scripts/gen_env_registry.py`` — the generator behind the CI doc-drift gate
    for ``docs/env-vars.md`` — statically scans for a string-literal argument, so a name
    built with an f-string or read through a variable resolves fine at runtime but is
    invisible to that scanner and silently escapes the documented env-var registry. Do not
    "tidy" this into an indirection (same rule as ``llm/model_classes.py::_env_override``)."""
    raw = os.environ.get("REBAR_LLM_CONFIG_FILE")
    if raw is None or not raw.strip():
        return None
    path = Path(raw.strip()).expanduser()
    if not path.is_file():
        raise ConfigError(f"REBAR_LLM_CONFIG_FILE points at {path}, which is not a readable file")
    return path


def layer_llm_config_file(discovered: dict) -> dict:
    """The ``[llm]`` table of the :func:`llm_config_file_pointer` file LAYERED over the
    already-discovered ``llm`` section (deep — see :func:`_deep_merge`), or ``discovered``
    unchanged when the pointer is unset.

    Layering rather than REPLACING (which is what ``KUBECONFIG`` / ``AWS_CONFIG_FILE`` /
    ``DOCKER_CONFIG`` do) is deliberate: an environment overriding one key must not have to
    restate the whole config, because that duplication drifts more readily than precedence
    subtlety does. The pointed file is an ordinary rebar config — a ``[llm]`` table, or
    ``[tool.rebar.llm]`` when it is named ``pyproject.toml`` — so a file with no such table
    (including an empty one) is a clean no-op, which is what makes it usable to make model
    resolution hermetic in tests."""
    path = llm_config_file_pointer()
    if path is None:
        return discovered
    sub = _read_toml_table(path, pyproject=(path.name == "pyproject.toml")).get("llm")
    return _deep_merge(discovered, sub) if isinstance(sub, dict) else discovered


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
    # verify.suggest_duplicate_tickets (story 9416): a pure rename, so the derived env
    # name moved with it and the pre-rename one is kept forever.
    "REBAR_VERIFY_OVERLAP_ENABLED": (
        "verify",
        "suggest_duplicate_tickets",
        "REBAR_VERIFY_SUGGEST_DUPLICATE_TICKETS",
    ),
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


# --------------------------------------------------------------------------- #
# Below-seam config resolvers (RP-04 config-ownership cutover, ticket d074).
# These OWN the ambient env / [snapshot]-config-table reads that previously sat
# BELOW the seam in the _store / _snapshot / _io helpers. Each below-seam caller
# now RECEIVES the resolved value from here (re-exported via ``rebar.config``)
# instead of reading ``os.environ`` itself. Every read is LIVE per call (never
# import-time bound) so a mid-operation override — e.g. the ``REBAR_SYNC_PUSH``
# toggle a bulk import drives — is still observed. This is the composition/raw-input
# seam the config-ownership gate treats as OWNED, so the env-name literals stay
# visible to ``gen_env_registry`` here.
# --------------------------------------------------------------------------- #


def _positive_int(raw: str | None, default: int) -> int:
    """Coerce a raw env value to a POSITIVE int, else ``default``.

    Anything not a positive integer — missing, blank, non-numeric, zero, negative —
    falls back to the default: a malformed knob must not crash a fetch, and a
    non-positive floor/window/attempt count would DISARM the stall protection. Callers
    pass ``os.environ.get("<LITERAL>")`` so every knob's name stays verbatim in the
    source for ``gen_env_registry``."""
    try:
        value = int((raw or "").strip())
    except (AttributeError, ValueError):
        return default
    return value if value > 0 else default


def resolve_stall_abort_limits(floor_default: int, window_default: int) -> tuple[int, int]:
    """``(floor, window)`` arming the fetch low-speed abort:
    ``REBAR_SNAPSHOT_STALL_FLOOR_BYTES_PER_SEC`` and
    ``REBAR_SNAPSHOT_STALL_WINDOW_SECONDS`` over the module defaults (live per call)."""
    return (
        _positive_int(os.environ.get("REBAR_SNAPSHOT_STALL_FLOOR_BYTES_PER_SEC"), floor_default),
        _positive_int(os.environ.get("REBAR_SNAPSHOT_STALL_WINDOW_SECONDS"), window_default),
    )


def resolve_stall_attempts(default: int) -> int:
    """In-process fetch retry budget: ``REBAR_SNAPSHOT_STALL_ATTEMPTS`` > ``default``
    (live per call, so a test/operator can retune without a reimport)."""
    return _positive_int(os.environ.get("REBAR_SNAPSHOT_STALL_ATTEMPTS"), default)


def resolve_gate_tmpdir() -> str:
    """Snapshot-store base override ``REBAR_GATE_TMPDIR``, or ``""`` when unset (the
    caller then falls back to ``tempfile.gettempdir()``)."""
    return os.environ.get("REBAR_GATE_TMPDIR") or ""


def resolve_allow_env_reidentify() -> bool:
    """``REBAR_ALLOW_ENV_REIDENTIFY`` re-identification acknowledgement — true for
    ``1``/``true``/``yes``/``on`` (case/space-insensitive), else false."""
    return os.environ.get("REBAR_ALLOW_ENV_REIDENTIFY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def resolve_lock_retries(default: int, max_retries: int) -> int:
    """Write-path retry passes: ``REBAR_LOCK_RETRIES`` (default ``default``) clamped to
    ``[0, max_retries]``. ``REBAR_LOCK_RETRIES=0`` restores fail-fast-on-one-budget; an
    unset/blank/unparseable value falls back to ``default`` so a malformed knob never
    breaks every write."""
    raw = os.environ.get("REBAR_LOCK_RETRIES")
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return max(0, min(value, max_retries))


def _snapshot_table(root: str | os.PathLike[str] | None = None) -> dict:
    """Merged raw ``[snapshot]`` config table (user < project), or ``{}`` if unreadable
    — a broken core config degrades the janitor to env/defaults, never breaks it."""
    try:
        merged: dict = {}
        up = user_config_path()
        if up.is_file():
            sub = _read_toml_table(up, pyproject=False).get("snapshot")
            if isinstance(sub, dict):
                merged.update(sub)
        proj = _discover_project_config(root)
        if proj is not None:
            path, kind = proj
            sub = _read_toml_table(path, pyproject=(kind == "pyproject")).get("snapshot")
            if isinstance(sub, dict):
                merged.update(sub)
        return merged
    except Exception:  # noqa: BLE001 - degrade to env/defaults on any config error
        return {}


def _snapshot_int(raw: str | None, table: dict, file_key: str, default: int) -> int:
    """One janitor int tunable: env ``raw`` > ``[snapshot]`` ``file_key`` > ``default``."""
    if raw is not None and raw.strip():
        try:
            return int(raw.strip())
        except ValueError:
            pass
    fv = table.get(file_key)
    if fv is not None and not isinstance(fv, bool):
        try:
            return int(fv)
        except (TypeError, ValueError):
            pass
    return default


def resolve_janitor_tunables(
    defaults: dict[str, int], root: str | os.PathLike[str] | None = None
) -> dict[str, int]:
    """Snapshot-cache janitor int tunables — ``REBAR_GATE_*`` env > ``[snapshot]``
    config table > per-knob ``defaults``. Owns the ambient env + config-table reads at
    the composition seam; the below-seam ``JanitorConfig`` RECEIVES these values."""
    t = _snapshot_table(root)
    return {
        "free_watermark_bytes": _snapshot_int(
            os.environ.get("REBAR_GATE_FREE_WATERMARK_BYTES"),
            t,
            "free_watermark_bytes",
            defaults["free_watermark_bytes"],
        ),
        "grace_seconds": _snapshot_int(
            os.environ.get("REBAR_GATE_GRACE_SECONDS"),
            t,
            "grace_seconds",
            defaults["grace_seconds"],
        ),
        "max_age_seconds": _snapshot_int(
            os.environ.get("REBAR_GATE_MAX_AGE_SECONDS"),
            t,
            "max_age_seconds",
            defaults["max_age_seconds"],
        ),
        "reverify_seconds": _snapshot_int(
            os.environ.get("REBAR_GATE_REVERIFY_SECONDS"),
            t,
            "reverify_seconds",
            defaults["reverify_seconds"],
        ),
        "interval_seconds": _snapshot_int(
            os.environ.get("REBAR_GATE_JANITOR_INTERVAL_SECONDS"),
            t,
            "interval_seconds",
            defaults["interval_seconds"],
        ),
    }


# --------------------------------------------------------------------------- #
# Below-seam CLI/command resolvers (RP-04 config-ownership cutover, ticket 9515).
# These OWN the ambient env reads that previously sat BELOW the composition seam in
# the ``_cli`` / ``_commands`` helpers. Each below-seam caller now RECEIVES the
# resolved value from here (re-exported via ``rebar.config``) instead of reading
# ``os.environ`` itself. All are entry-time (the CLI resolves each once at entry), so
# a plain resolver is correct. Leaf-safe: they touch only stdlib, so this module keeps
# its no-``rebar.config``-import invariant. The env-name literals stay visible to
# ``gen_env_registry`` here.
# --------------------------------------------------------------------------- #


def repo_root_or_none(explicit: str | os.PathLike[str] | None = None) -> str | None:
    """Repo root with precedence ``explicit`` > ``REBAR_ROOT`` > git toplevel of cwd,
    or ``None`` when NONE resolve — the NOT-FOUND signal the init bootstraps need.

    Mirrors :func:`repo_root`'s precedence but REPORTS not-found instead of defaulting
    to ``Path.cwd()`` (both init call sites must distinguish "no repo" to emit the
    "not a git repository" error / return ``None``). ``explicit`` and ``REBAR_ROOT`` are
    realpath-normalized; the git toplevel is returned verbatim. Owns the ``REBAR_ROOT``
    read at the composition seam."""
    if explicit:
        return os.path.realpath(str(explicit))
    env = os.environ.get("REBAR_ROOT")
    if env:
        return os.path.realpath(env)
    try:
        cp = subprocess.run(
            ["git", "-C", ".", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_DISCOVERY_ACROSS_FILESYSTEM": "1"},
            check=False,
        )
    except OSError:
        return None
    out = cp.stdout.strip()
    return out if cp.returncode == 0 and out else None


def resolve_otlp_endpoint(explicit: str | None = None) -> str:
    """The OTLP trace-sink endpoint: an explicit ``--otlp-endpoint`` wins, else the
    standard ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var, else ``""`` (no sink). Owns the
    env read for :func:`rebar._cli._llm_eval_commands`'s config-init snippet."""
    return explicit or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or ""


def resolve_detected_by(explicit: str | None = None) -> str | None:
    """The detection-channel value: an explicit param wins (an explicit empty string
    SUPPRESSES the env default), else the ``REBAR_DETECTED_BY`` env var, else ``None``.
    Owns the env read for :func:`rebar._commands.composer`'s detection-channel capture;
    the caller still strips/normalizes and drops an empty result."""
    return explicit if explicit is not None else os.environ.get("REBAR_DETECTED_BY")


def resolve_os_actor(tracker: str | os.PathLike[str]) -> str:
    """The audit actor for a store operation: the git ``user.email`` configured for the
    ``tracker`` dir (``git -C <tracker> config user.email``), else ``$USER``, else
    ``"unknown"``. Shared owned seam for the ``_actor`` helpers in
    :mod:`rebar._commands.bridge_repair` and :mod:`rebar._commands.tracker_maintenance`
    — it TAKES the tracker dir so each caller keeps its dir-scoped identity."""
    try:
        result = subprocess.run(
            ["git", "-C", str(tracker), "config", "user.email"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except OSError:
        pass
    return os.environ.get("USER") or "unknown"


# --------------------------------------------------------------------------- #
# Below-seam LLM-subsystem resolvers (RP-04 config-ownership cutover, ticket 1b07).
# These OWN the ambient env / ``[snapshot]``-config-table reads that previously sat BELOW
# the composition seam in the ``llm`` / ``llm/plan_review`` / ``llm/workflow`` helpers.
# Each below-seam caller now RECEIVES the resolved value from here (re-exported via
# ``rebar.config``) instead of reading ``os.environ`` itself. Every read is LIVE per call
# (never import-time bound) so a mid-operation override — the gate ``ref``/``source``, the
# plan-review budget, the usage-log sink, the preview timeout an operator flips per run — is
# still observed. This is the composition seam the config-ownership gate treats as OWNED, so
# the env-name literals stay visible to ``gen_env_registry`` here. Callers pass any subsystem
# default constant (``DEFAULT_REF`` / ``SOURCE_ATTESTED`` / …) so this leaf module keeps its
# stdlib-only, no-``rebar``-import invariant.
# --------------------------------------------------------------------------- #


def _gate_str_pref(
    env_name: str,
    file_key: str,
    default: str,
    root: str | os.PathLike[str] | None = None,
) -> str:
    """One gate string preference: env ``env_name`` > ``[snapshot]`` ``file_key`` > ``default``,
    trimmed, re-read LIVE per call (an env override applied mid-operation is observed)."""
    raw = os.environ.get(env_name)
    if raw is not None and raw.strip():
        return raw.strip()
    fv = _snapshot_table(root).get(file_key)
    if isinstance(fv, str) and fv.strip():
        return fv.strip()
    return default


def resolve_gate_ref(default: str, root: str | os.PathLike[str] | None = None) -> str:
    """The code-reading gate's default ``ref``: ``REBAR_GATE_REF`` > ``[snapshot].ref`` >
    ``default`` (the caller's ``DEFAULT_REF``). LIVE per call so a mid-run override lands.
    Owns the ambient read for :func:`rebar.llm.gate_source.default_ref`."""
    return _gate_str_pref("REBAR_GATE_REF", "ref", default, root)


def resolve_gate_source(default: str, root: str | os.PathLike[str] | None = None) -> str:
    """The code-reading gate's default ``source`` preference: ``REBAR_GATE_SOURCE`` >
    ``[snapshot].source`` > ``default`` (the caller's ``SOURCE_ATTESTED``), LIVE per call.
    The caller still validates the value against the allowed modes. Owns the ambient read
    for :func:`rebar.llm.gate_source.default_source`."""
    return _gate_str_pref("REBAR_GATE_SOURCE", "source", default, root)


def resolve_plan_review_budget(default: float) -> float:
    """The base per-plan budget cap in USD, BEFORE centrality scaling:
    ``REBAR_PLAN_REVIEW_BUDGET`` when a parseable float, else ``default``. Read LIVE per call
    (a malformed value silently falls back). Owns the ambient read for
    :func:`rebar.llm.plan_review.sizing.plan_budget_cap`."""
    raw = os.environ.get("REBAR_PLAN_REVIEW_BUDGET", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return default


def resolve_usage_log_sink() -> str | None:
    """The explicit usage-log JSONL sink override ``REBAR_USAGE_LOG``, or ``None`` when unset
    (the caller then falls back to its gate-session default sink). Read LIVE per call. Owns the
    ambient read for :func:`rebar.llm.usage_log`'s sink resolution."""
    path = os.environ.get("REBAR_USAGE_LOG")
    return path or None


def resolve_preview_timeout(default: float) -> float:
    """The criterion-preview sync-attempt budget in seconds: ``REBAR_PREVIEW_TIMEOUT`` when a
    POSITIVE number, else ``default`` (the caller's ``_DEFAULT_TIMEOUT``). Read LIVE per call.
    Owns the ambient read for :func:`rebar.llm.workflow.criterion_preview._default_timeout`."""
    raw = os.environ.get("REBAR_PREVIEW_TIMEOUT")
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return default


def resolve_run_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    """The run-scoped scratch base for the workflow-run bank: ``explicit`` > ``REBAR_ROOT`` >
    ``Path.cwd()`` (env>cwd — NOT the git-toplevel fallback of :func:`repo_root`, and paths are
    kept verbatim, NOT realpath-normalized, to preserve the exact scratch dir the run and the
    repo-leak guard expect). Owns the ``REBAR_ROOT`` read for
    :func:`rebar.llm.workflow.completion_banking.CriterionBank.for_run`."""
    if explicit:
        return Path(explicit)
    env_root = os.environ.get("REBAR_ROOT")
    return Path(env_root) if env_root else Path.cwd()


# --------------------------------------------------------------------------- #
# Reconciler JIRA-family resolvers — owned entry points for the below-seam
# adapters. Each references ``load_config`` as a ``rebar.config`` attribute at
# CALL time (monkeypatch-compatible) and imports it LAZILY, so the stdlib-only
# reconciler engine stays importable without ``rebar`` on the path.
# --------------------------------------------------------------------------- #


def resolve_jira_connection() -> tuple[str, str, str]:
    """``(url, user, project)`` from ``load_config().jira.*``; on ``ConfigError`` fall
    back to the ``JIRA_URL``/``JIRA_USER``/``JIRA_PROJECT`` env layer (read LIVE per call).
    ``InsecureUrlError`` PROPAGATES — a cleartext ``jira.url`` must fail loud, not degrade."""
    import rebar.config as _cfg

    try:
        jira = _cfg.load_config().jira
        return jira.url, jira.user, jira.project
    except _cfg.InsecureUrlError:
        raise
    except _cfg.ConfigError:
        return (
            os.environ.get("JIRA_URL", ""),
            os.environ.get("JIRA_USER", ""),
            os.environ.get("JIRA_PROJECT", ""),
        )


def resolve_acli_call_timeout(default: int) -> int:
    """The ACLI per-call subprocess timeout: ``reconciler.jira_cli_timeout`` over
    ``default``; a ``ConfigError`` or any non-positive value falls back to ``default``."""
    import rebar.config as _cfg

    try:
        value = _cfg.load_config().reconciler.jira_cli_timeout
    except _cfg.ConfigError:
        return default
    return value if value > 0 else default


def resolve_jira_probe_scope(env: Mapping[str, str] | None) -> tuple[str, str, str]:
    """``(url, user, project)`` from the ``JIRA_URL``/``JIRA_USER``/``JIRA_PROJECT`` env
    layer, reading ``env`` when supplied else the live process environment."""
    src: Mapping[str, str] = os.environ if env is None else env
    return src.get("JIRA_URL", ""), src.get("JIRA_USER", ""), src.get("JIRA_PROJECT", "")


def resolve_dc_comment_max_chars() -> int:
    """The Data Center comment ceiling ``reconciler.comment_max_chars`` — FAIL LOUD (no
    ``ConfigError`` guard); a non-positive value is returned as ``0`` (= unlimited)."""
    import rebar.config as _cfg

    configured = _cfg.load_config().reconciler.comment_max_chars
    return configured if configured > 0 else 0


def resolve_dc_connection() -> tuple[str, str, bool, str]:
    """``(base_url, project, allow_insecure, ca_bundle)`` from the typed config — FAIL
    LOUD (any ``ConfigError`` propagates rather than degrading to env-only defaults)."""
    import rebar.config as _cfg

    config = _cfg.load_config()
    reconciler = config.reconciler
    return (
        reconciler.base_url,
        config.jira.project,
        reconciler.allow_insecure,
        reconciler.ca_bundle,
    )


def resolve_rich_text_cutover() -> frozenset[str]:
    """The set of clients on the RICH rich-text wire from ``reconciler.rich_text_cutover``
    (``off``→∅, ``cloud``/``dc``→that one, ``both``→both), read LIVE per call. Raises
    ``ConfigError`` when the config is unreadable — the caller fails CLOSED to ``∅``."""
    import rebar.config as _cfg

    value = _cfg.load_config().reconciler.rich_text_cutover
    if value == "both":
        return frozenset({"cloud", "dc"})
    if value in ("cloud", "dc"):
        return frozenset({value})
    return frozenset()


def resolve_pandoc_timeout(default: float) -> float:
    """The pandoc wall-clock ceiling ``reconciler.dc_pandoc_timeout_s`` over ``default``
    (read LIVE per call); a non-positive value falls back to ``default``. May raise
    ``ConfigError``/``AttributeError``/``TypeError``/``ValueError`` — the caller fails safe."""
    import rebar.config as _cfg

    value = float(_cfg.load_config().reconciler.dc_pandoc_timeout_s)
    return value if value > 0 else default


_DEFAULT_ABSENT_RETIRE_GRACE = 3


def resolve_absent_retire_grace() -> int:
    """Consecutive-404 retire grace (``RECONCILER_ABSENT_RETIRE_GRACE``): raw env over the
    default, clamped to ``>= 1``. Owns the read for ``binding_store``/``binding_walk``."""
    raw = os.environ.get("RECONCILER_ABSENT_RETIRE_GRACE")
    if raw is None:
        return _DEFAULT_ABSENT_RETIRE_GRACE
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_ABSENT_RETIRE_GRACE
