"""rebar root/config resolution (Python side), mirroring ``_engine/rebar-config.sh``."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

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
    coerce_sparse,
    merge_sparse,
)
from rebar._config_schema import CodeHealthConfig as CodeHealthConfig
from rebar._config_schema import CompactConfig as CompactConfig
from rebar._config_schema import EnsureConfig as EnsureConfig
from rebar._config_schema import InsecureUrlError as InsecureUrlError
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

# The raw-input resolution layer (repo-root/config-file location, the TOML parse
# cache, project/user config discovery, and the ``REBAR_<SECTION>_<KEY>`` env-override
# layer incl. legacy env aliases) lives in the sibling module rebar._config_sources.
# Import the names config.py uses, then re-export every other moved name so
# `from rebar.config import X` keeps working (public API).
from rebar._config_sources import _CANONICAL_ENV_NAMES as _CANONICAL_ENV_NAMES
from rebar._config_sources import (
    _LEGACY_ENV_ALIASES,
    _TOML_CACHE,
    _canonical_env_name,
    _discover_project_config,
    _read_toml_table,
    _strict_unknown_keys,
    env_overrides,
    repo_root,
    tracker_dir_override,
    user_config_path,
)
from rebar._config_sources import _map_legacy_env as _map_legacy_env
from rebar._config_sources import _parse_toml as _parse_toml
from rebar._config_sources import _pyproject_rebar_state as _pyproject_rebar_state
from rebar._config_sources import config_file as config_file
from rebar._config_sources import layer_llm_config_file as layer_llm_config_file
from rebar._config_sources import repo_root_or_none as repo_root_or_none
from rebar._config_sources import resolve_absent_retire_grace as resolve_absent_retire_grace
from rebar._config_sources import resolve_acli_call_timeout as resolve_acli_call_timeout
from rebar._config_sources import (
    resolve_allow_env_reidentify as resolve_allow_env_reidentify,
)
from rebar._config_sources import resolve_dc_comment_max_chars as resolve_dc_comment_max_chars
from rebar._config_sources import resolve_dc_connection as resolve_dc_connection
from rebar._config_sources import resolve_detected_by as resolve_detected_by
from rebar._config_sources import resolve_fetch_timeout as resolve_fetch_timeout
from rebar._config_sources import resolve_gate_ref as resolve_gate_ref
from rebar._config_sources import resolve_gate_source as resolve_gate_source
from rebar._config_sources import resolve_gate_tmpdir as resolve_gate_tmpdir
from rebar._config_sources import resolve_janitor_tunables as resolve_janitor_tunables
from rebar._config_sources import resolve_jira_connection as resolve_jira_connection
from rebar._config_sources import resolve_jira_probe_scope as resolve_jira_probe_scope
from rebar._config_sources import resolve_lock_retries as resolve_lock_retries
from rebar._config_sources import resolve_os_actor as resolve_os_actor
from rebar._config_sources import resolve_otlp_endpoint as resolve_otlp_endpoint
from rebar._config_sources import resolve_pandoc_timeout as resolve_pandoc_timeout
from rebar._config_sources import resolve_plan_review_budget as resolve_plan_review_budget
from rebar._config_sources import resolve_preview_timeout as resolve_preview_timeout
from rebar._config_sources import resolve_rich_text_cutover as resolve_rich_text_cutover
from rebar._config_sources import resolve_run_root as resolve_run_root
from rebar._config_sources import resolve_stall_abort_limits as resolve_stall_abort_limits
from rebar._config_sources import resolve_stall_attempts as resolve_stall_attempts
from rebar._config_sources import resolve_usage_log_sink as resolve_usage_log_sink
from rebar._config_writer import _emit_config_toml as _emit_config_toml
from rebar._config_writer import _emit_nested_toml as _emit_nested_toml

# The config WRITE path — the ONLY writer of a rebar-owned ``rebar.toml`` (the TOML emitter
# and the read-whole/mutate/re-emit/atomic-replace ``[jira]`` writer, ADR 0070) — lives in the
# sibling module rebar._config_writer. Everything left in this module reads or resolves config.
# Re-exported (INCLUDING the private ``_emit_toml``, which the onboarding tests reach as
# ``cfg._emit_toml``) so `from rebar.config import X` and the module-attribute form both keep
# working (public API).
from rebar._config_writer import _emit_toml as _emit_toml
from rebar._config_writer import write_jira_config as write_jira_config
from rebar._operation_config import ENVELOPE_VERSION as ENVELOPE_VERSION
from rebar._operation_config import OperationSnapshot as OperationSnapshot
from rebar._operation_config import active_snapshot
from rebar._operation_config import bind_operation_snapshot as bind_operation_snapshot
from rebar._operation_config import (
    compose_and_bind_operation_snapshot as compose_and_bind_operation_snapshot,
)
from rebar._operation_config import compose_operation_snapshot as compose_operation_snapshot

# Plan-review criteria authoring-guide deep-links (epic cite-stone-sea / WS10). A NARROW env-read
# — the base URL for the generated guide's per-criterion anchors — NOT a typed TOML config key
# (deep-links are a plan-review rendering concern, not core config surface).
# The canonical hosted guide (pyproject [project.urls] Documentation points at the same tree). A
# consumer install has no rebar ``docs/`` tree, so a repo-relative ``file://`` default produced a
# dead link out of the box (client report §5); the hosted URL resolves for consumers, and GitHub
# slugs a ``## <criterion-id>`` heading to ``#<criterion-id lower-cased>`` — the anchor form below.
_DEFAULT_DOCS_URL = "https://github.com/navapbc/rebar/blob/main/docs/plan-review-criteria-guide.md"


def plan_review_docs_url(_explicit_root: str | os.PathLike[str] | None = None) -> str:
    """Base URL for the plan-review criteria authoring guide (no trailing ``#anchor``):
    ``REBAR_DOCS_URL`` if set, else the canonical hosted guide. The default is deliberately
    root-independent so coaching deep-links resolve from a consumer install (which has no rebar
    ``docs/`` tree). ``explicit_root`` is accepted for signature stability but no longer consulted
    for the default."""
    env = os.environ.get("REBAR_DOCS_URL", "").strip()
    if env:
        return env.rstrip("/")
    return _DEFAULT_DOCS_URL


def plan_review_guide_anchor(
    criterion_id: str, explicit_root: str | os.PathLike[str] | None = None
) -> str:
    """A stable deep-link to a criterion's guide section: ``<base>#<criterion-id lower-cased>``
    (the anchor matches the guide's ``## <criterion-id>`` heading slug)."""
    return f"{plan_review_docs_url(explicit_root)}#{criterion_id.lower()}"


def _bound_snapshot_for_root(root: str | os.PathLike[str] | None) -> OperationSnapshot | None:
    """The active operation's bound :class:`OperationSnapshot`, but ONLY when it was
    composed for the SAME repository *root* this call is resolving for (RP-04 S2;
    AC3 "explicit operation input remains authoritative"). ``root=None`` means "resolve
    for whatever this call would ambiently pick" — exactly what the bound snapshot
    already captured — so it always qualifies; an EXPLICIT root that resolves to a
    DIFFERENT repository (an unusual cross-repo utility call) bypasses the binding and
    falls through to the caller's own fresh ambient resolution for that other root."""
    snapshot = active_snapshot()
    if snapshot is None:
        return None
    if root is None:
        return snapshot
    try:
        if str(repo_root(root)) == snapshot.repo_root:
            return snapshot
    except Exception:  # noqa: BLE001 — resolution failure falls through to the legacy path
        return None
    return None


def _config_value(root: str | os.PathLike[str] | None, section: str, key: str) -> object:
    snapshot = _bound_snapshot_for_root(root)
    if snapshot is not None:
        return snapshot.values[section][key]
    return getattr(getattr(load_config(root), section), key)


def tracker_dir(root: str | os.PathLike[str] | None = None) -> Path:
    """Path to the ticket event store, resolved through the full config precedence:
    the explicit env override (``REBAR_TRACKER_DIR``) wins verbatim; otherwise the
    configured ``tracker.dir``
    (``-c`` flag > project/user config > default ``.tickets-tracker``) — an absolute
    value relocates the store (EV-3b), a relative one is the dir name under the repo
    root. Previously this consulted env only; it now reads the typed config too.

    When an operation snapshot is bound for this root (RP-04 S2: the CLI/MCP/command
    entry points compose-and-bind ONE snapshot per operation), the configured value is
    read from THAT already-resolved snapshot instead of a fresh ``load_config`` — so a
    later env/project/CWD mutation cannot change the tracker dir mid-operation. With no
    snapshot bound (a bare library call outside those entry points, or a malformed
    config the binding swallowed), this falls back to the pre-existing ambient read.

    The repository ROOT a relative name is joined against is frozen the same way: a
    bound snapshot's own ``repo_root`` is used verbatim rather than re-resolving
    ``root`` ambiently, so a mid-operation ``REBAR_ROOT``/CWD move cannot silently
    re-anchor an otherwise-frozen relative tracker dir onto a different repository."""
    env = tracker_dir_override()
    if env:
        return Path(env)
    snapshot = _bound_snapshot_for_root(root)
    if snapshot is not None:
        name = snapshot.values["tracker"]["dir"]
        base = Path(snapshot.repo_root)
    else:
        try:
            name = load_config(root).tracker.dir
        except ConfigError:
            # Locating the store must not be coupled to config validity (it was
            # env-only before): a malformed config falls back to the default name.
            # The fail-closed gates (close/verify) surface the ConfigError via their
            # own load_config. Relocation is applied BY THIS FUNCTION: REBAR_TRACKER_DIR
            # already returned above, and an absolute tracker.dir is returned verbatim
            # below. Only a config that will not parse reaches here, and then there is
            # no configured value.
            # tickets-boundary-ok: the ConfigError-only default INSIDE tracker_dir() itself
            name = ".tickets-tracker"
        base = repo_root(root)
    return Path(name) if os.path.isabs(name) else base / name


def tickets_branch(root: str | os.PathLike[str] | None = None) -> str:
    """The orphan git branch the ticket event log lives on (and the basis for its
    ``origin/<branch>`` ref), resolved through the full config precedence: the
    configured ``tracker.branch`` (``-c`` flag > ``REBAR_TRACKER_BRANCH`` env >
    project/user config > default ``tickets``). The single source of the branch name
    for every git path (init/sync/push/reconciler/fsck/reads).

    Unlike :func:`tracker_dir`, a malformed config is NOT swallowed here: silently
    defaulting the branch could mis-route writes to the wrong branch (a data-integrity
    risk), so the ``ConfigError`` propagates and the operation fails loudly.

    Reads from the bound operation snapshot when one is active for this root (see
    :func:`tracker_dir`'s snapshot note); otherwise resolves live via ``load_config``."""
    return str(_config_value(root, "tracker", "branch"))


def tickets_remote(root: str | os.PathLike[str] | None = None) -> str:
    """The git remote the ticket event-log branch syncs to — push, fetch/reconcile, the
    ``fsck`` PUSH_PENDING check, and the attested ticket-store materialization — resolved
    through the full config precedence (``-c`` flag > ``REBAR_SYNC_REMOTE`` env >
    project/user config > default ``origin``). The single source of the remote name for
    every ticket git-network path; the remote counterpart to :func:`tickets_branch`.

    Reads from the bound operation snapshot when one is active for this root (see
    :func:`tracker_dir`'s snapshot note); otherwise resolves live via ``load_config``.

    Split-residency setups (code reviewed on a ``gerrit`` remote; the tickets branch's
    source of truth on a ``github``/``origin`` remote for a downstream sync) set this so
    the store no longer hard-assumes ``origin`` is the ticket remote. Like
    :func:`tickets_branch`, a malformed config is NOT swallowed here: silently defaulting
    could mis-route a push to the wrong remote, so the ``ConfigError`` propagates."""
    return str(_config_value(root, "sync", "remote"))


def reclaim_horizon_days(root: str | os.PathLike[str] | None = None) -> int:
    return int(str(_config_value(root, "reclaim", "horizon_days")))


def fixture_heal_interval_days(root: str | os.PathLike[str] | None = None) -> int:
    return int(str(_config_value(root, "fixture_heal", "interval_days")))


# ── config-file discovery + layered load ──────────────────────────────────────
# Config resolution is on the COMMAND HOT PATH. Two caches keep it cheap and bounded:
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


# The precedence layers, lowest to highest. ``defaults`` is not a layer — it is
# applied once by Config.from_mapping after the sparse layers merge.
LAYER_ORDER: tuple[str, ...] = ("default", "user", "project", "env", "cli")


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
    # Every tombstoned (REMOVED) env var — a retired var appearing mid-process must
    # invalidate the cache so the top-of-load_config tombstone scan re-fires.
    from rebar._deprecations import tombstones

    for ri in tombstones():
        if ri.kind == "env":
            sig.append((ri.name, os.environ.get(ri.name)))
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
    # Tombstone checks for REMOVED env vars + the legacy flat config file, BEFORE the
    # cache lookup so they fire even on a WARM cache hit (a retired input must never be
    # silently served from cache). ``raise_or_warn_env`` raises RemovedInputError for
    # an error-class env var and WARNs for a warn-class one (skip_llm: the LLM-scoped
    # env tombstones are enforced in rebar.llm.config, not on every core resolve).
    from rebar._deprecations import raise_or_warn_env, raise_or_warn_file, tombstones

    raise_or_warn_env(os.environ)
    # File tombstones route through the registry helper like the env and cfg kinds
    # (bug d064): the row's `behavior` decides raise-vs-warn, and detection is driven
    # by the registry rather than a hardcoded filename — so a file entry added later,
    # or an existing one downgraded to `warn`, is honoured without touching this call.
    _root = repo_root(root)
    _retired = [ri.name for ri in tombstones() if ri.kind == "file" and (_root / ri.name).exists()]
    if _retired:
        raise_or_warn_file(_retired)

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
    ``[tool.rebar.mcp] readonly`` file key (``load_config`` layers env above file). On a
    MALFORMED config it raises :class:`ConfigError` — chained from the parse fault and
    naming the gate — so the fault surfaces to the operator instead of reading as the
    read-only POLICY choice (operator ruling 39f8-ae7c: "Unreadable config should result
    in an error"; the pre-ruling fail-closed ``return True`` silently laundered the
    fault into a policy value). Both read-only call sites route through this — the MCP
    server's write-tool gating (``mcp_server._readonly``) and the LLM runner's
    comment-tool withholding (``runner._readonly_gate``) — so the two cannot diverge
    (they once did: the runner read only the env var and ignored the file key)."""
    try:
        return load_config().mcp.readonly
    except ConfigError as exc:
        raise ConfigError(
            f"cannot resolve the MCP read-only gate: the rebar config could not be "
            f"read ({exc}). An unreadable config is an error (operator ruling "
            "39f8-ae7c) — fix the config file, then retry the operation."
        ) from exc


def mcp_gate(attr: str) -> bool:
    """THE owned composition-root resolver for a typed ``mcp.<attr>`` boolean gate
    (``allow_llm`` / ``allow_jira_sync`` / any future one). Resolves through the
    single-source typed config — env ``REBAR_MCP_<ATTR>`` wins over the
    ``[tool.rebar.mcp]`` file key. On a MALFORMED config it raises
    :class:`ConfigError` — chained from the parse fault and naming the gate — per
    operator ruling 39f8-ae7c (the former ``fail`` keyword existed only to pick a
    malformed-config fallback and was removed with that ruling: a fault must error,
    never silently resolve to a default, even a safe one). The MCP server's per-request
    gate check (``mcp_server._mcp_gate``) routes here so it RECEIVES composed config
    instead of reading ``load_config`` below the composition seam (sibling of
    :func:`mcp_readonly`)."""
    try:
        return bool(getattr(load_config().mcp, attr))
    except ConfigError as exc:
        raise ConfigError(
            f"cannot resolve the mcp.{attr} gate: the rebar config could not be "
            f"read ({exc}). An unreadable config is an error (operator ruling "
            "39f8-ae7c) — fix the config file, then retry the operation."
        ) from exc


def compose_config(root: str | os.PathLike[str] | None = None) -> Config:
    """Composition-root entry for a process root (e.g. the MCP server's ``build_server``
    / ``main``) or a CLI command arm to obtain the fully-composed typed :class:`Config`
    for a repo ``root`` (``None`` = discover from cwd). Routing the load through this
    owned seam lets those roots and the ``_cli`` / ``_commands`` helpers RECEIVE composed
    config instead of calling ``load_config`` below the composition seam. A
    :class:`ConfigError` propagates (fail-closed startup), exactly as a direct
    ``load_config`` would."""
    return load_config() if root is None else load_config(root)


def environment_id_or_none() -> str | None:
    """Raw ``REBAR_ENV_ID`` passthrough (``None`` when unset); no default, no error.
    Owned accessor for the reconciler's ``last_pass`` identity reads — the caller keeps
    its own set-but-empty handling (``resolve_environment_id`` raises ``LastPassError``;
    ``publish`` forwards ``None`` verbatim)."""
    return os.environ.get("REBAR_ENV_ID")


def repo_root_env() -> str | None:
    """Raw ``REBAR_ROOT`` passthrough (``None`` when unset). Owned accessor for the
    read-path ``tracker_dir`` resolver, which distinguishes a supplied/env root from an
    absent one before its git-work-tree precondition."""
    return os.environ.get("REBAR_ROOT")


def _is_repo_checkout(root: Path) -> bool:
    """A candidate ``root`` is a real rebar checkout when it has BOTH a ``.git`` entry
    (a ``.git`` dir OR a worktree ``.git`` file) AND the package dir ``src/rebar/``. This
    pair is exactly what a venv/site-packages tree lacks."""
    return (root / ".git").exists() and (root / "src" / "rebar").is_dir()


def _git_toplevel_of_cwd() -> Path | None:
    """The git toplevel of the current working directory, or ``None`` when the CWD is not
    inside a git work tree, ``git`` is unavailable, or the probe fails/hangs.

    Any subprocess failure — a missing ``git`` binary (``OSError``), a non-zero exit
    (CWD not in a repo), or a timeout on a slow/networked filesystem — degrades to
    ``None`` so :func:`reconciler_repo_root` falls through to its clear step-4 error
    rather than surfacing an opaque traceback."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode == 0 and out.stdout.strip():
        return Path(out.stdout.strip())
    return None


def reconciler_repo_root() -> Path:
    """Repo root for the reconciler pass, resolved as a VALIDATED LAYERED FALLBACK:

    1. ``REBAR_ROOT`` when set → returned verbatim as ``Path(env)`` (the operator's
       explicit choice, honored as-is and never validated).
    2. else the deterministic, CWD-INDEPENDENT package root
       (``Path(__file__).resolve().parents[2]``) IF it is a real checkout — this preserves
       the editable/source-install path and the daemon-from-anywhere guarantee (the package
       root is always tried before any CWD-based resolution).
    3. else the git toplevel of the current working directory IF that is a real checkout —
       a last-resort convenience for a non-editable dev install launched from inside a
       checkout.
    4. else raise a clear error — NEVER silently return a non-checkout path (e.g. the
       ``site-packages`` tree of a wheel install).

    The daemon-from-anywhere / CWD-independence rationale still holds: the package root is
    tried before the CWD, so whenever it is a valid checkout (or ``REBAR_ROOT`` is set) the
    result never depends on the caller's CWD. An arbitrary non-checkout CWD errors clearly
    rather than reconciling the wrong tree."""
    env = os.environ.get("REBAR_ROOT")
    if env:
        return Path(env)
    # reconciler_repo_root is THE validated resolver for the reconciler's repo root:
    # pkg-root-seam: parents[2] is used only after _is_repo_checkout confirms a real checkout.
    package_root = Path(__file__).resolve().parents[2]
    if _is_repo_checkout(package_root):
        return package_root
    cwd_root = _git_toplevel_of_cwd()
    if cwd_root is not None and _is_repo_checkout(cwd_root):
        return cwd_root
    raise RuntimeError(
        "Cannot determine the rebar repo root for the reconciler: the package root "
        f"{package_root} is not a checkout (a non-editable/wheel install lives in "
        "site-packages), and the current working directory is not inside a rebar "
        "checkout. Set REBAR_ROOT=<checkout> or pass --repo-root <checkout>."
    )


def reconciler_event_identity() -> tuple[str, str]:
    """``(env_id, author)`` for reconciler-authored events. ``REBAR_ENV_ID`` /
    ``REBAR_AUTHOR`` default to the legacy-Jira signature when UNSET (a set-but-empty
    value stays ``''``; never raises). Owned accessor for ``inbound_translate``."""
    from rebar.reducer._version import LEGACY_JIRA_AUTHOR, LEGACY_JIRA_ENV_ID

    return (
        os.environ.get("REBAR_ENV_ID", LEGACY_JIRA_ENV_ID),
        os.environ.get("REBAR_AUTHOR", LEGACY_JIRA_AUTHOR),
    )


def resolve_jira_detection() -> tuple[str, str, str, bool]:
    """Resolve the current (non-secret) Jira coordinates + whether the secret token is
    set, for the onboarding wizard's in-process detection. Returns
    ``(url, user, project, token_present)``.

    ``load_config().jira`` ALREADY layers the ``JIRA_URL`` / ``JIRA_USER`` /
    ``JIRA_PROJECT`` env over the config file, so this owns that composition once (no
    below-seam re-read); a :class:`ConfigError` degrades to the empty/env-only coordinates
    (fail-soft, matching the reconciler resolver — a bad ``jira.url`` must not block the
    wizard that FIXES it). The secret ``JIRA_API_TOKEN`` is resolved through the
    credential seam :func:`read_secret_env` and reported only as a presence flag — the
    value never leaves this boundary."""
    url = user = project = ""
    try:
        jira = load_config().jira
        url, user, project = jira.url, jira.user, jira.project
    except ConfigError:
        pass
    # bool() keeps this a PRESENCE check: the secret value is discarded here and never
    # leaves this boundary (see the docstring above). The marker must sit on the READING
    # line itself — the ownership gate looks only at that one line.
    token_present = bool(read_secret_env("JIRA_API_TOKEN"))  # read-via: credential-presence
    return url, user, project, token_present


def resolve_push_mode(root: str | os.PathLike[str] | None = None) -> str:
    """Outbound tickets-branch push policy, resolved LIVE per call: ``sync.push`` (env
    ``REBAR_SYNC_PUSH`` or a config file) normalized to ``always`` | ``async`` | ``off``;
    a malformed config falls back to ``always`` so a bad config never silently disables
    the auto-push. Owned seam for :func:`rebar._store.push._push_mode`.

    LIVE per call is load-bearing (NOT composed once and threaded): a bulk
    :func:`rebar._io.import_ndjson` toggles ``REBAR_SYNC_PUSH=off`` in the process env
    around its interior writes to defer per-event pushes, then restores it for one final
    push — a compose-once value would stop observing that mutation and break the defer."""
    from rebar._store._push_policy import normalize_push_mode

    try:
        return normalize_push_mode(load_config(root=root).sync.push)
    except ConfigError:
        return "always"


def read_secret_env(env_name: str) -> str:
    """Resolve a secret from the environment variable NAMED by ``env_name`` at the config
    composition boundary, returning ``""`` when unset/empty. THE owned seam for the MCP
    auth verifiers and the proxy-app wrap: the secret VALUE is resolved HERE (a
    composition root) and kept in-memory only, so those below-seam call sites never touch
    ``os.environ`` themselves. NEVER log the return value — route auth diagnostics through
    :func:`rebar._mcp_auth.redact`."""
    return os.environ.get(env_name) or ""


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
