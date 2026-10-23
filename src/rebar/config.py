"""rebar root/config resolution (Python side), mirroring ``_engine/rebar-config.sh``."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from rebar._config_schema import _ALIASES as _ALIASES
from rebar._config_schema import _RESERVED_SECTIONS as _RESERVED_SECTIONS
from rebar._config_schema import _SECTION_CLASSES as _SECTION_CLASSES

# Re-export the split schema's public API while importing local resolver needs.
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

# Re-export raw-input discovery, parse caching, and env override helpers from
# ``_config_sources`` so established ``rebar.config`` imports still work.
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
from rebar._config_sources import resolve_rich_text_cutover as resolve_rich_text_cutover
from rebar._config_sources import resolve_run_root as resolve_run_root
from rebar._config_sources import resolve_stall_abort_limits as resolve_stall_abort_limits
from rebar._config_sources import resolve_stall_attempts as resolve_stall_attempts
from rebar._config_sources import resolve_usage_log_sink as resolve_usage_log_sink
from rebar._config_writer import _emit_config_toml as _emit_config_toml
from rebar._config_writer import _emit_nested_toml as _emit_nested_toml

# ``_config_writer`` alone writes rebar.toml; re-export its public writer and
# test-used ``_emit_toml`` while this module owns reads and resolution.
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

# Guide deep-links use a narrow env override and hosted default, since installed
# consumers lack the repo docs tree. Criterion headings supply lowercase anchors.
_DEFAULT_DOCS_URL = "https://github.com/navapbc/rebar/blob/main/docs/plan-review-criteria-guide.md"


def plan_review_docs_url(_explicit_root: str | os.PathLike[str] | None = None) -> str:
    """Use ``REBAR_DOCS_URL`` or the root-independent hosted guide URL.

    The root parameter remains for call compatibility; return no trailing anchor.
    """
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
    """Use the active snapshot only for its own repository root.

    ``root=None`` accepts the bound ambient root; an explicit different root
    falls back to its own resolution.
    """
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
    """Resolve the ticket store from env override, typed config, or default.

    Absolute paths relocate the store; relative paths join the repo root. A bound
    operation snapshot freezes both configured path and root for this operation.
    Without one, read ambient config; malformed config falls back to the default.
    """
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
            # Store location falls back on malformed config; close/verify gates
            # surface their own config error rather than inheriting this fallback.
            # tickets-boundary-ok: the ConfigError-only default INSIDE tracker_dir() itself
            name = ".tickets-tracker"
        base = repo_root(root)
    return Path(name) if os.path.isabs(name) else base / name


def tickets_branch(root: str | os.PathLike[str] | None = None) -> str:
    """Resolve the ticket branch from bound snapshot or layered config.

    Propagate malformed config: guessing a default branch could misroute writes.
    """
    return str(_config_value(root, "tracker", "branch"))


def tickets_remote(root: str | os.PathLike[str] | None = None) -> str:
    """Resolve the ticket remote from bound snapshot or layered config.

    This covers split code and ticket remotes. Propagate malformed config rather
    than risk pushing to a guessed destination.
    """
    return str(_config_value(root, "sync", "remote"))


def reclaim_horizon_days(root: str | os.PathLike[str] | None = None) -> int:
    return int(str(_config_value(root, "reclaim", "horizon_days")))


def fixture_heal_interval_days(root: str | os.PathLike[str] | None = None) -> int:
    return int(str(_config_value(root, "fixture_heal", "interval_days")))


# ── config-file discovery + layered load ──────────────────────────────────────
# Parsed TOML is keyed by file metadata; resolved config is keyed by root,
# environment, and CLI input. Warm hits re-stat known and probed paths so edits
# or new higher-priority files invalidate them; errors are never cached.
# value: (config, validation) where validation is a tuple of file stat-tokens.
_RESULT_CACHE: dict[tuple, tuple[Config, tuple]] = {}

# CLI ``-c`` sets this highest-precedence layer once per process. MCP never
# mutates it per request, so the shared value does not race MCP calls.
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
    """Clear both config caches and process CLI overrides for a fresh resolution."""
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
    """Build user, project, env, and CLI layers in precedence order.

    Return the discovered project source too; values and provenance share layers.
    """
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
    """Hash discovery pointers, overrides, aliases, and tombstones for cache keys."""
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
    """List present and absent config candidates to detect later file creation.

    Capture these paths only on a cold resolve; warm hits re-stat them.
    """
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
    """Resolve config and file tokens for read and candidate paths."""
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
    """Resolve typed config: defaults < user < project < env < CLI.

    Defaults apply once after sparse layers merge. Cache hits re-stat config
    candidates; errors propagate and are not cached. An explicit empty
    ``cli_overrides`` bypasses process-wide CLI overrides.
    """
    # Check retired inputs before cache lookup, including warm hits. LLM-specific
    # tombstones remain in LLM config.
    from rebar._deprecations import raise_or_warn_env, raise_or_warn_file, tombstones

    raise_or_warn_env(os.environ)
    # Registry behavior, not a hardcoded filename, decides file warning/refusal.
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
    """Resolve the shared MCP read-only gate through typed config.

    Env overrides file config. A malformed config raises ``ConfigError`` rather
    than masquerading as a read-only policy; MCP and LLM tool gating share it.
    """
    try:
        return load_config().mcp.readonly
    except ConfigError as exc:
        raise ConfigError(
            f"cannot resolve the MCP read-only gate: the rebar config could not be "
            f"read ({exc}). An unreadable config is an error (operator ruling "
            "39f8-ae7c) — fix the config file, then retry the operation."
        ) from exc


def mcp_gate(attr: str) -> bool:
    """Resolve typed MCP boolean gates with env-over-file precedence.

    Malformed config raises a named ``ConfigError`` instead of selecting a
    fallback value.
    """
    try:
        return bool(getattr(load_config().mcp, attr))
    except ConfigError as exc:
        raise ConfigError(
            f"cannot resolve the mcp.{attr} gate: the rebar config could not be "
            f"read ({exc}). An unreadable config is an error (operator ruling "
            "39f8-ae7c) — fix the config file, then retry the operation."
        ) from exc


def compose_config(root: str | os.PathLike[str] | None = None) -> Config:
    """Compose typed config for a process or CLI root, propagating config errors."""
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
    """Return the CWD's git root, or ``None`` on absence, failure, or timeout."""
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
    """Resolve reconciler root from explicit env, package checkout, then CWD checkout.

    The package root precedes CWD for daemon stability. Reject non-checkout
    install paths instead of reconciling the wrong tree.
    """
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
    """Return Jira URL, user, project, and token-presence for onboarding.

    Malformed config leaves empty coordinates so setup can repair it. Only a
    boolean token presence leaves the secret boundary.
    """
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
    """Resolve ``always``/``async``/``off`` live for each ticket push.

    Bulk import changes the env mid-operation to defer per-event pushes.
    Malformed config falls back to ``always``.
    """
    from rebar._store._push_policy import normalize_push_mode

    try:
        return normalize_push_mode(load_config(root=root).sync.push)
    except ConfigError:
        return "always"


def read_secret_env(env_name: str) -> str:
    """Read an in-memory secret at the config boundary; never log its value.

    Return ``""`` when unset so MCP auth callers avoid direct env reads.
    """
    return os.environ.get(env_name) or ""


def read_config_file(path: str | os.PathLike[str]) -> Config:
    """Load one explicit TOML or pyproject file without other config layers.

    Invalid or unreadable input raises ``ConfigError``.
    """
    p = Path(path)
    if p.name == "pyproject.toml":
        raw = _read_toml_table(p, pyproject=True)
    else:
        raw = _read_toml_table(p, pyproject=False)
    return Config.from_mapping(raw, source=str(p), strict=_strict_unknown_keys())


def read_reserved_section(name: str, root: str | os.PathLike[str] | None = None) -> dict:
    """Merge a reserved section's raw user and project tables without core coercion.

    The owning layer applies its typing and env/CLI overrides. Propagate file
    errors for the caller to handle.
    """
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
    """Return raw CLI overrides for one reserved section, or ``{}``."""
    sub = (_CLI_OVERRIDES or {}).get(name)
    return dict(sub) if isinstance(sub, dict) else {}


def resolve_with_sources(
    root: str | os.PathLike[str] | None = None, *, cli_overrides: dict | None = None
) -> tuple[Config, dict[str, dict[str, str]], tuple[Path, str] | None]:
    """Return typed config, each key's winning layer, and the project source.

    Share ``load_config`` layers so displayed provenance matches resolved values.
    """
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
