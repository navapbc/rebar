"""Central registry + signalling for rebar's user-facing deprecations.

This module is the **single source of truth** for every deprecated user-facing
surface — env vars, config keys, CLI flags, library aliases, and MCP tools —
replacing the old DOC-ONLY prose table that used to live in ``config.py``. Each
entry records where it is going and *when*:

* ``key``         — a unique, stable id string (``"<kind>:<name>"``).
* ``kind``        — one of ``env`` / ``cfg`` / ``cli`` / ``lib`` / ``mcp``.
* ``name``        — the human name of the deprecated surface (used in the message).
* ``replacement`` — the canonical thing to use instead.
* ``remove_in``   — the release the surface is scheduled to be removed in
  (e.g. ``"v1.0.0"``); ``None`` for a **permanent** alias.
* ``permanent``   — ``True`` for an ergonomic rename kept forever (no removal
  planned), ``False`` for a scheduled removal.

Every runtime deprecation signal in rebar routes through :func:`warn_deprecated`,
which looks the key up here and **raises** if it is absent — so a new deprecated
surface that skips the registry is caught by
``tests/unit/test_deprecation_registry.py`` (which also source-scans for raw
``is deprecated`` / ``DeprecationWarning`` emissions bypassing this helper).

The message wording depends on ``permanent``: a scheduled entry says
"…is deprecated; use <replacement> (scheduled for removal in <remove_in>)";
a permanent entry says "…is a permanent alias of <replacement>" and NEVER claims
to be "deprecated" (resolving the historical contradiction where permanent
ergonomic renames still warned "deprecated").
"""

from __future__ import annotations

import logging
import sys
import warnings
from collections.abc import Mapping
from dataclasses import dataclass

# Default logger for log-channel signals. Config/LLM callers pass their own module
# logger (``rebar.config`` / ``rebar.llm.config``) so existing log-capture tests
# and namespacing are preserved.
_DEFAULT_LOGGER = logging.getLogger("rebar")

_KINDS = frozenset({"env", "cfg", "cli", "lib", "mcp"})

# The horizon every scheduled 0.x back-compat surface is retired at: the first
# MAJOR boundary where dropping a still-honored input is semver-legal.
_MAJOR = "v1.0.0"


@dataclass(frozen=True)
class Deprecation:
    """One deprecated user-facing surface. See the module docstring for fields."""

    key: str
    kind: str
    name: str
    replacement: str
    remove_in: str | None
    permanent: bool


def _scheduled(kind: str, name: str, replacement: str, *, remove_in: str = _MAJOR) -> Deprecation:
    return Deprecation(f"{kind}:{name}", kind, name, replacement, remove_in, permanent=False)


def _permanent(kind: str, name: str, replacement: str) -> Deprecation:
    return Deprecation(f"{kind}:{name}", kind, name, replacement, None, permanent=True)


# ── The registry ──────────────────────────────────────────────────────────────
# EVERY deprecated user-facing surface with a runtime signal lives here. When you
# add a deprecation, add a row here AND route its warning through warn_deprecated;
# the completeness test fails otherwise.
_ENTRIES: tuple[Deprecation, ...] = (
    # ── env aliases: PERMANENT ergonomic renames (no removal planned) ──────────
    # These are stable REBAR_-prefixed renames of established names; they warned
    # "deprecated" historically, which was a contradiction — they are kept forever.
    # This registry now holds ONLY these permanent renames: every remaining
    # SCHEDULED (removable) surface has been removed in the pre-1.0 breaking passes.
    _permanent("env", "REBAR_NO_SYNC", "REBAR_SYNC_PULL"),
    _permanent("env", "COMPACT_THRESHOLD", "REBAR_COMPACT_THRESHOLD"),
    _permanent("env", "SCRATCH_BASE_DIR", "REBAR_SCRATCH_BASE_DIR"),
    _permanent("env", "REBAR_ACLI_TIMEOUT", "REBAR_JIRA_CLI_TIMEOUT"),
    _permanent("env", "RECONCILER_ABSENT_GET_BUDGET", "REBAR_RECONCILER_DELETION_PROBE_LIMIT"),
    _permanent("env", "REBAR_ID_GUARD_MODE", "REBAR_UNSAFE_ID_GUARD_BYPASS"),
    # ── removed scheduled surfaces (historical record) ─────────────────────────
    # NOTE (DE7): the first pre-1.0 breaking removal dropped the scheduled env
    # aliases REBAR_PUSH / TICKETS_TRACKER_DIR / REBAR_MCP_ALLOW_RECONCILE_LIVE, the
    # config surface verify.require_verdict_for_close + the flat .rebar/config.conf
    # reader, the CLI --verdict-hash flag, and the lib edit_ticket(tags=...) alias +
    # rebar.list_epics() function.
    # NOTE (this pass, ticket 5899): the second breaking removal dropped the env
    # alias REBAR_LLM_MAX_ITERS (use REBAR_LLM_MAX_STEPS), the accepted-but-ignored
    # config value reconciler.lock_backend='file' (the whole key is gone — the ref
    # backend is the only backend), the CLI list-epics subcommand + --no-sync alias
    # (use `list --type=epic …` / --no-pull), and the MCP list_epics tool (use
    # list_tickets(ticket_type='epic', …)).
)

REGISTRY: dict[str, Deprecation] = {d.key: d for d in _ENTRIES}


# ── Tombstone registry (story 36c7): fail-LOUD for REMOVED inputs ─────────────
# Distinct from the alias registry above (which still HONORS the old surface). A
# tombstone names an input that has been fully REMOVED. When one is still set —
# an env var, a TOML key, or a legacy config file — rebar must not silently ignore
# it: an operationally-load-bearing removed input (store location, write/sync gate,
# auth, security, lifecycle policy) FAILS LOUD (``behavior="error"`` → a targeted
# migration error + non-zero exit), while an operationally-inert one WARNs and
# continues (``behavior="warn"``). This is a SEPARATE vocabulary from ``_KINDS`` —
# do not overload it.
_TOMBSTONE_KINDS = frozenset({"env", "cfg", "file"})
_TOMBSTONE_BEHAVIORS = frozenset({"error", "warn"})


class RemovedInputError(BaseException):
    """A removed, still-set, operationally load-bearing input was detected.

    Subclasses :class:`BaseException` (NOT :class:`Exception`) DELIBERATELY: the
    config → tracker → MCP path is riddled with ``except ConfigError`` / broad
    ``except Exception`` fallbacks that swallow a bad config into a silent default.
    A ``BaseException`` sails through all of those, so a retired input can never be
    demoted into a silent fallback — it surfaces as a targeted migration error with
    a non-zero exit at the CLI/MCP boundary."""

    def __init__(self, removed: RemovedInput | str):
        self.removed = removed if isinstance(removed, RemovedInput) else None
        msg = removed if isinstance(removed, str) else _tombstone_message(removed)
        super().__init__(msg)


@dataclass(frozen=True)
class RemovedInput:
    """One fully-removed user-facing input (see the tombstone-registry note).

    * ``kind``        — one of :data:`_TOMBSTONE_KINDS` (``env`` / ``cfg`` / ``file``).
    * ``name``        — the removed surface (env var name / ``section.key`` / file path).
    * ``replacement`` — what to use instead (``""`` for removed-with-no-replacement).
    * ``removed_in``  — the release the surface was removed in.
    * ``behavior``    — :data:`_TOMBSTONE_BEHAVIORS`: ``error`` fails loud, ``warn`` logs.
    """

    kind: str
    name: str
    replacement: str
    removed_in: str
    behavior: str

    def __post_init__(self) -> None:
        if self.kind not in _TOMBSTONE_KINDS:
            raise ValueError(f"RemovedInput: bad kind {self.kind!r}")
        if self.behavior not in _TOMBSTONE_BEHAVIORS:
            raise ValueError(f"RemovedInput: bad behavior {self.behavior!r}")


def _tombstone_message(ri: RemovedInput) -> str:
    """The user-facing migration message for a removed input."""
    if ri.replacement:
        return (
            f"{ri.name} was removed in {ri.removed_in} — use {ri.replacement} instead "
            "(migrate your config/env)"
        )
    return f"{ri.name} was removed in {ri.removed_in} (no replacement; it is now ignored)"


def _tomb(kind: str, name: str, replacement: str, behavior: str) -> RemovedInput:
    return RemovedInput(
        kind=kind, name=name, replacement=replacement, removed_in=_MAJOR, behavior=behavior
    )


# The seeded tombstones. Grouped by kind/behavior for readability; the ORDER here is
# the reporting order for ``rebar config validate``.
_TOMBSTONE_REGISTRY: tuple[RemovedInput, ...] = (
    # env, error — load-bearing (store location / MCP sync gate).
    _tomb("env", "TICKETS_TRACKER_DIR", "REBAR_TRACKER_DIR", "error"),
    _tomb("env", "REBAR_MCP_ALLOW_RECONCILE_LIVE", "REBAR_MCP_ALLOW_JIRA_SYNC", "error"),
    # env, warn — inert renames / dropped tunables.
    _tomb("env", "REBAR_PUSH", "REBAR_SYNC_PUSH", "warn"),
    _tomb("env", "REBAR_RECONCILER_LOCK_MAX_RETRIES", "", "warn"),
    _tomb("env", "REBAR_RECONCILER_LOCK_RETRY_BUDGET", "", "warn"),
    # cfg, error — lifecycle/close gate rename.
    _tomb(
        "cfg",
        "verify.require_verdict_for_close",
        "verify.require_completion_verification_for_close",
        "error",
    ),
    # cfg, warn — dropped reconciler lock tunables.
    _tomb("cfg", "reconciler.lock_backend", "", "warn"),
    _tomb("cfg", "reconciler.lock_max_retries", "", "warn"),
    # file, error — the legacy flat config reader.
    _tomb("file", ".rebar/config.conf", "rebar.toml [tool.rebar]", "error"),
    # env, error (llm) — retired LLM step-budget knob (checked in llm.config.from_env).
    _tomb("env", "REBAR_LLM_MAX_ITERS", "REBAR_LLM_MAX_STEPS", "error"),
)


def tombstones() -> tuple[RemovedInput, ...]:
    """The full tombstone registry (accessor for callers that scan it)."""
    return _TOMBSTONE_REGISTRY


def tombstone_for(kind: str, name: str) -> RemovedInput | None:
    """The registered tombstone matching ``(kind, name)``, or ``None``."""
    for ri in _TOMBSTONE_REGISTRY:
        if ri.kind == kind and ri.name == name:
            return ri
    return None


def removed_input(kind: str, name: str) -> RemovedInput:
    """The registered tombstone matching ``(kind, name)`` — for a raise site that KNOWS
    the tombstone exists (raises KeyError on a wiring typo, never returns ``None``)."""
    ri = tombstone_for(kind, name)
    if ri is None:  # pragma: no cover - internal wiring invariant
        raise KeyError(f"no tombstone registered for {kind}:{name}")
    return ri


def _signal_tombstone(ri: RemovedInput) -> None:
    """Fail loud (raise) for an error-class tombstone; WARN (log on the ``rebar``
    channel) for a warn-class one. The warn path reuses the same logging channel as
    :func:`warn_deprecated`'s ``via="log"`` emission."""
    if ri.behavior == "error":
        raise RemovedInputError(ri)
    _DEFAULT_LOGGER.warning(_tombstone_message(ri))


def raise_or_warn_env(env: Mapping[str, str], *, skip_llm: bool = True) -> None:
    """For each env-kind tombstone present in ``env``, raise (error) or WARN (warn).

    ``skip_llm`` (default) skips the ``[tool.rebar.llm]``-scoped env tombstones (whose
    names begin ``REBAR_LLM_``) — those are enforced inside ``rebar.llm.config`` so a
    non-LLM command is not aborted by a retired LLM knob. The core config-layer scan
    passes ``skip_llm=True``; the LLM layer checks its own var directly."""
    for ri in _TOMBSTONE_REGISTRY:
        if ri.kind != "env":
            continue
        if skip_llm and ri.name.startswith("REBAR_LLM_"):
            continue
        if ri.name in env:
            _signal_tombstone(ri)


def raise_or_warn_cfg_key(sect: str, key: str) -> RemovedInput | None:
    """If ``sect.key`` is a cfg-kind tombstone, raise (error) or WARN (warn) and
    return the matched tombstone (so the caller drops the key); else ``None``."""
    ri = tombstone_for("cfg", f"{sect}.{key}")
    if ri is not None:
        _signal_tombstone(ri)
    return ri


def raise_or_warn_file(present: list) -> None:
    """For each file-kind tombstone whose registry name appears in ``present`` (the
    list of legacy files that EXIST, by their registry name), raise or WARN."""
    names = {str(p) for p in present}
    for ri in _TOMBSTONE_REGISTRY:
        if ri.kind == "file" and ri.name in names:
            _signal_tombstone(ri)


def scan_tombstones(
    *, env: dict, toml_tables: dict, file_paths: list
) -> list[tuple[RemovedInput, str]]:
    """NON-raising sweep for ``rebar config validate``: return ALL matching tombstones
    as ``(RemovedInput, observed_context)``. ``env`` is the environment mapping,
    ``toml_tables`` a ``{section: {key: value}}`` nested mapping of parsed config, and
    ``file_paths`` the list of legacy files present (by their registry name). Reports
    every match rather than aborting on the first, so the operator sees the whole
    migration surface at once."""
    hits: list[tuple[RemovedInput, str]] = []
    present_files = {str(p) for p in file_paths}
    for ri in _TOMBSTONE_REGISTRY:
        if ri.kind == "env":
            if ri.name in env:
                hits.append((ri, ri.name))
        elif ri.kind == "cfg":
            sect, _, key = ri.name.partition(".")
            if key in (toml_tables.get(sect) or {}):
                hits.append((ri, ri.name))
        elif ri.kind == "file":
            if ri.name in present_files:
                hits.append((ri, ri.name))
    return hits


def _message(dep: Deprecation) -> str:
    """The user-facing signalling message for ``dep`` (wording keys off ``permanent``)."""
    if dep.permanent:
        return f"{dep.name} is a permanent alias of {dep.replacement} (no removal planned)."
    return (
        f"{dep.name} is deprecated; use {dep.replacement} "
        f"(scheduled for removal in {dep.remove_in})."
    )


def warn_deprecated(
    key: str,
    *,
    logger: logging.Logger | None = None,
    via: str = "log",
    stacklevel: int = 2,
) -> str:
    """Emit the standard deprecation signal for the registered surface ``key``.

    Looks ``key`` up in :data:`REGISTRY` and **raises** ``KeyError`` if it is
    absent — this is what makes the completeness test real: an emission site whose
    surface is not catalogued cannot signal through here. Returns the message.

    ``via`` selects the emission channel to match each call site's historical one:

    * ``"log"``     — ``logger.warning(msg)`` (default; pass ``logger`` for the
      caller's namespace so log-capture tests keep working).
    * ``"warning"`` — ``warnings.warn(msg, DeprecationWarning)``.
    * ``"stderr"``  — ``sys.stderr.write(msg + "\\n")`` (CLI parse-boundary signals).
    """
    dep = REGISTRY[key]  # KeyError on an unregistered surface — intentional (completeness).
    msg = _message(dep)
    if via == "warning":
        warnings.warn(msg, DeprecationWarning, stacklevel=stacklevel + 1)
    elif via == "stderr":
        sys.stderr.write(msg + "\n")
    elif via == "log":
        (logger or _DEFAULT_LOGGER).warning(msg)
    else:  # pragma: no cover - guards against a typo'd channel
        raise ValueError(f"unknown deprecation channel via={via!r}")
    return msg
