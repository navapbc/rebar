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
    # ── env aliases: scheduled for removal (the rename-window aliases) ─────────
    _scheduled("env", "REBAR_PUSH", "REBAR_SYNC_PUSH"),
    _scheduled("env", "TICKETS_TRACKER_DIR", "REBAR_TRACKER_DIR"),
    _scheduled("env", "REBAR_MCP_ALLOW_RECONCILE_LIVE", "REBAR_MCP_ALLOW_JIRA_SYNC"),
    # REBAR_LLM_MAX_ITERS: documented as a "deprecated alias" of REBAR_LLM_MAX_STEPS
    # with no stated horizon — classified as SCHEDULED (per ticket 5274: treat any
    # surface you cannot confidently prove permanent as scheduled) at the v1.0.0
    # major boundary shared by the other scheduled removals.
    _scheduled("env", "REBAR_LLM_MAX_ITERS", "REBAR_LLM_MAX_STEPS"),
    # ── env aliases: PERMANENT ergonomic renames (no removal planned) ──────────
    # These are stable REBAR_-prefixed renames of established names; they warned
    # "deprecated" historically, which was a contradiction — they are kept forever.
    _permanent("env", "REBAR_NO_SYNC", "REBAR_SYNC_PULL"),
    _permanent("env", "COMPACT_THRESHOLD", "REBAR_COMPACT_THRESHOLD"),
    _permanent("env", "SCRATCH_BASE_DIR", "REBAR_SCRATCH_BASE_DIR"),
    _permanent("env", "REBAR_ACLI_TIMEOUT", "REBAR_JIRA_CLI_TIMEOUT"),
    _permanent("env", "RECONCILER_ABSENT_GET_BUDGET", "REBAR_RECONCILER_DELETION_PROBE_LIMIT"),
    _permanent("env", "REBAR_ID_GUARD_MODE", "REBAR_UNSAFE_ID_GUARD_BYPASS"),
    # ── config-key surfaces ───────────────────────────────────────────────────
    _scheduled("cfg", "verify.require_verdict_for_close", "verify.require_signature_for_close"),
    _scheduled(
        "cfg",
        "flat .rebar/config.conf reader",
        "rebar.toml or a [tool.rebar] table in pyproject.toml",
    ),
    # reconciler.lock_backend='file' is accepted-but-ignored (the file backend was
    # removed in epic dust-troth-naval / ADR 0031). No explicit horizon exists;
    # classified as scheduled (the config key acceptance is retired at v1.0.0).
    _scheduled(
        "cfg",
        "reconciler.lock_backend='file'",
        "the refs/reconciler/* lock backend (remove the key)",
    ),
    # ── CLI surfaces ──────────────────────────────────────────────────────────
    _scheduled(
        "cli",
        "list-epics",
        "rebar list --type=epic --status=open,in_progress --unblocked [--min-children=N]",
    ),
    _scheduled("cli", "--verdict-hash", "rebar sign <id> <manifest>"),
    _scheduled("cli", "--no-sync", "--no-pull"),
    # ── library surfaces ──────────────────────────────────────────────────────
    _scheduled("lib", "edit_ticket(tags=...)", "set_tags / add_tags / remove_tags"),
    _scheduled("lib", "rebar.list_epics()", "list_tickets(ticket_type='epic', ...)"),
    # ── MCP surfaces ──────────────────────────────────────────────────────────
    _scheduled("mcp", "list_epics", "the list_tickets tool (ticket_type='epic', ...)"),
)

REGISTRY: dict[str, Deprecation] = {d.key: d for d in _ENTRIES}


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
