"""rebar below-seam config RESOLVERS -- the RP-04 config-ownership entry points.

Split out of :mod:`rebar._config_sources` (a pure structural split; no behavior change).
That module is the RAW-INPUT resolution layer -- repo-root and config-file location, the
mtime-keyed TOML parse cache, project/user config discovery, and the
``REBAR_<SECTION>_<KEY>`` env-override layer. THIS module is the other concern the source
already drew in-file: the below-seam OWNED RESOLVERS the RP-04 config-ownership cutover
introduced (tickets d074, 9515, 1b07, plus the reconciler JIRA family). Each one OWNS an
ambient env / ``[snapshot]``-config-table / ``load_config`` read that used to sit BELOW the
composition seam in a ``_store`` / ``_snapshot`` / ``_cli`` / ``_commands`` / ``llm`` /
reconciler helper; every below-seam caller now RECEIVES the resolved value from here.

``rebar._config_sources`` re-exports every name defined here, and ``rebar.config`` re-exports
them in turn, so the public import surface is unchanged (``from rebar.config import X`` and
``rebar._config_sources.X`` both still work).

This module is a LEAF with NO module-level ``rebar`` import:

* it keeps the re-export in :mod:`rebar._config_sources` acyclic, and
* it keeps the stdlib-only reconciler engine able to import it.

The raw-input names one resolver needs (:func:`_snapshot_table`) are therefore reached
through a LAZY in-body ``from rebar import _config_sources`` and called as module
attributes at CALL time -- the same ``_config_sources.__dict__`` slot a module-global
lookup used before the split, so a ``monkeypatch.setattr`` on that module still steers
this code. Binding them eagerly at import time is the exact regression the sibling
``config.py`` split shipped (a patched ``repo_root`` silently stopped applying). The
reconciler family likewise imports ``rebar.config`` lazily in-body.

Env-name string LITERALS stay verbatim in these bodies: ``scripts/gen_env_registry.py``
scans for them statically, and ``scripts/check_config_ownership.py`` classifies this file
as a composition root by its BASENAME, so the reads here are owned, not below-seam.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

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


def resolve_fetch_timeout(default: int) -> int:
    """Materialization-fetch wall-clock backstop:
    ``REBAR_SNAPSHOT_FETCH_TIMEOUT_SECONDS`` over the module default (live per call).

    This ceiling is NOT the guard against a wedged remote — the throughput-keyed
    stall-abort (:func:`resolve_stall_abort_limits`) is, and it trips a dead connection in
    seconds regardless of this value. The wall clock only backstops a hang the low-speed
    check cannot see (a pre-transport wedge that moves zero bytes, e.g. a stuck credential
    helper). So it is deliberately GENEROUS and TUNABLE: a large/cold store whose HONEST
    ``--no-filter`` transfer legitimately runs minutes must not be cut off purely because a
    fixed 300s wall clock elapsed (bug curly-open-swan). A malformed/non-positive knob falls
    back to ``default`` rather than disarming the backstop."""
    return _positive_int(os.environ.get("REBAR_SNAPSHOT_FETCH_TIMEOUT_SECONDS"), default)


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
    — a broken core config degrades the janitor to env/defaults, never breaks it.

    The three raw-input helpers are reached as ATTRIBUTES of :mod:`rebar._config_sources`,
    imported lazily in-body. That is deliberate, not incidental: the attribute lookup hits
    the same module ``__dict__`` slot the pre-split module-global lookup did, so late
    binding survives the move and a ``monkeypatch.setattr`` on that module still steers
    this function. A module-level ``from ... import`` would bind at import time (silently
    defeating such a patch) AND cycle with that module's re-export of this one."""
    from rebar import _config_sources

    try:
        merged: dict = {}
        up = _config_sources.user_config_path()
        if up.is_file():
            sub = _config_sources._read_toml_table(up, pyproject=False).get("snapshot")
            if isinstance(sub, dict):
                merged.update(sub)
        proj = _config_sources._discover_project_config(root)
        if proj is not None:
            path, kind = proj
            sub = _config_sources._read_toml_table(path, pyproject=(kind == "pyproject")).get(
                "snapshot"
            )
            if isinstance(sub, dict):
                merged.update(sub)
        return merged
    except Exception:  # noqa: BLE001 - degrade to env/defaults on any config error
        return {}


def _snapshot_int(raw: str | None, table: dict, key: str, defaults: dict[str, int]) -> int:
    """One janitor int tunable: env ``raw`` > ``[snapshot]`` ``key`` > ``defaults[key]``.

    Every janitor knob names its ``[snapshot]`` key and its default with the SAME string, so
    the caller hands over the whole defaults mapping and this indexes it — one name per knob
    at the call site instead of two that could silently drift apart."""
    if raw is not None and raw.strip():
        try:
            return int(raw.strip())
        except ValueError:
            pass
    fv = table.get(key)
    if fv is not None and not isinstance(fv, bool):
        try:
            return int(fv)
        except (TypeError, ValueError):
            pass
    return defaults[key]


def resolve_janitor_tunables(
    defaults: dict[str, int], root: str | os.PathLike[str] | None = None
) -> dict[str, int]:
    """Snapshot-cache janitor int tunables — ``REBAR_GATE_*`` env > ``[snapshot]``
    config table > per-knob ``defaults``. Owns the ambient env + config-table reads at
    the composition seam; the below-seam ``JanitorConfig`` RECEIVES these values."""
    t = _snapshot_table(root)
    d = defaults
    return {
        "free_watermark_bytes": _snapshot_int(
            os.environ.get("REBAR_GATE_FREE_WATERMARK_BYTES"), t, "free_watermark_bytes", d
        ),
        # Volume-RELATIVE free-space headroom, whole percent (bug 3a52). INT like every other
        # janitor knob — a fractional 0.2 would be truncated to 0 here and silently disable the
        # term rather than fail, so the knob is points (20 = reclaim at 80% used).
        "free_watermark_pct": _snapshot_int(
            os.environ.get("REBAR_GATE_FREE_WATERMARK_PCT"), t, "free_watermark_pct", d
        ),
        "grace_seconds": _snapshot_int(
            os.environ.get("REBAR_GATE_GRACE_SECONDS"), t, "grace_seconds", d
        ),
        "max_age_seconds": _snapshot_int(
            os.environ.get("REBAR_GATE_MAX_AGE_SECONDS"), t, "max_age_seconds", d
        ),
        # Store-size cap in BYTES (ADR 0005's byte-total backstop). 0 = off, like
        # free_watermark_pct and reverify_seconds — an unset deployment keeps today's
        # free-space-only behaviour.
        "max_bytes": _snapshot_int(os.environ.get("REBAR_GATE_MAX_BYTES"), t, "max_bytes", d),
        "reverify_seconds": _snapshot_int(
            os.environ.get("REBAR_GATE_REVERIFY_SECONDS"), t, "reverify_seconds", d
        ),
        "interval_seconds": _snapshot_int(
            os.environ.get("REBAR_GATE_JANITOR_INTERVAL_SECONDS"), t, "interval_seconds", d
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
