"""Idempotent desired-state ensures for initialized stores.

Ensure units replace init-only correction steps with repeatable check-then-act
operations. They are not an ordered migration ledger. See ``docs/migrations.md``.
Each unit has a stable identifier and returns :class:`EnsureOutcome`, with no
change when its state is already converged.

:func:`run_ensures` executes every unit under the store write lock. Concurrent
sweeps serialize, failures are recorded and skipped, and a converged sweep makes
no Git commit. An atomic rewrite of the ignored ``.ensure-applied`` marker records
all nonfailed identifiers. The marker informs the write-path warning and the
``rebar fsck`` applied count, but never controls execution.

Content-backed units live in :mod:`rebar._commands._init_ensures` and remain
re-exported from :mod:`rebar._commands.init`. That module also owns
``untrack-runtime-markers``. Lazy imports keep ``registry_ids`` and
``applied_ids`` independent of the init command on write paths.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Literal

from rebar._store import fsutil

# Resolve ``canonical_tracker`` through the module at call time. A by-value import
# can retain a test monkeypatch after teardown and direct ensures to the wrong store
# (bug d720-fc72).
from rebar._store import lock as _lock
from rebar._store.compat import StoreIncompatibleError

logger = logging.getLogger("rebar")

# Git-ignored marker (JSON array of non-failed unit ids). Absent/garbage → empty set.
APPLIED_MARKER = ".ensure-applied"

# Git-ignored marker (WS2): a single last-hinted unix timestamp that rate-limits the
# write-path pending nudge. Absent/garbage → "never hinted".
HINTED_MARKER = ".ensure-hinted"

EnsureStatus = Literal["ok", "changed", "failed"]


@dataclass(frozen=True)
class EnsureOutcome:
    """The typed result of one ensure unit: ``ok`` (already converged, no-op),
    ``changed`` (drift corrected), or ``failed`` (the unit raised; skipped)."""

    id: str
    status: EnsureStatus
    detail: str = ""


# Stable identifiers persist in ``.ensure-applied`` and support the pending hint
# and ``fsck`` count without importing ``init``. Tests require ``_registry()`` to
# cover this exact set because renaming an identifier makes every store pending.
REGISTRY_IDS: tuple[str, ...] = (
    "env-id",
    "gc-config",
    "merge-ours",
    "gitattributes",
    "gitignore",
    "store-compat",
    "projects-seed",
    "projects-compat-stamp",
    "untrack-runtime-markers",
)


def registry_ids() -> frozenset[str]:
    """The frozen set of registered ensure-unit ids (no ``init`` import — safe on
    the write hot-path and the read-only ``fsck`` line)."""
    return frozenset(REGISTRY_IDS)


def _registry() -> dict[str, object]:
    """Map id → check-then-act callable. Lazy-imports the unit implementations from
    :mod:`rebar._commands.init` (cold path — only :func:`run_ensures` calls this)."""
    from rebar._commands import init
    from rebar._store import env_identity, project_ensures

    return {
        # env-id lives in `env_identity` (not `init`) because minting is guarded: it
        # refuses to invent an identity for a store that already holds another
        # environment's events (bug gold-distinct-lacewing).
        "env-id": env_identity.ensure_env_id_unit,
        "gc-config": init._gc_config_unit,
        "merge-ours": init._merge_ours_unit,
        "gitattributes": init._gitattributes_unit,
        "gitignore": init._gitignore_unit,
        "store-compat": init._store_compat_unit,
        "projects-seed": project_ensures.seed_projects_mapping_unit,
        "projects-compat-stamp": project_ensures.converge_multi_project_stamp_unit,
        "untrack-runtime-markers": init._untrack_runtime_markers_unit,
    }


def _applied_path(tracker: str) -> str:
    return os.path.join(tracker, APPLIED_MARKER)


def applied_ids(tracker: str | os.PathLike) -> set[str]:
    """Parse ``.ensure-applied`` → set of applied unit ids. Absent/garbage/any
    non-list JSON degrades to the EMPTY set (never raises) — a pre-feature or
    corrupt marker simply reads as 'everything pending'."""
    try:
        with open(_applied_path(_lock.canonical_tracker(tracker)), encoding="utf-8") as fh:
            raw = fh.read()
        data = json.loads(raw)
    except (OSError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    return {str(x) for x in data}


def _write_applied(tracker: str, ids: list[str]) -> None:
    """Atomically rewrite ``.ensure-applied`` with the CURRENT non-failed id set —
    a full rewrite (never read-modify-write) via :func:`fsutil.atomic_write`
    (temp-in-same-dir + os.replace, so never a cross-device rename), so a torn/lost
    write is impossible and the set always reflects this sweep. Best-effort: the
    marker is only a hint, so a write failure is swallowed and never fails the sweep."""
    try:
        fsutil.atomic_write(_applied_path(tracker), json.dumps(sorted(set(ids))))
    except OSError as exc:
        logger.warning("run_ensures: could not write %s: %s", APPLIED_MARKER, exc)


def run_ensures(
    tracker: str | os.PathLike,
    *,
    timeout: int | None = None,
    attempts: int | None = None,
) -> list[EnsureOutcome]:
    """Run every ensure under the write lock and return each outcome.

    Unit failures become ``failed`` outcomes and are omitted from
    ``.ensure-applied``. Lock acquisition or unexpected sweep failures are logged
    as no-ops so init and boot continue. The store compatibility gate is the only
    propagated exception, which keeps init, MCP boot, and ``fsck --repair``
    fail-closed for an incompatible store.

    ``timeout`` and ``attempts`` bound lock acquisition. ``None`` preserves the
    lock defaults, while latency-sensitive callers can supply a shorter budget.
    """
    tracker = _lock.canonical_tracker(tracker)
    lock_kwargs: dict[str, Any] = {}
    if timeout is not None:
        lock_kwargs["timeout"] = timeout
    if attempts is not None:
        lock_kwargs["attempts"] = attempts
    outcomes: list[EnsureOutcome] = []
    try:
        with _lock.write_lock(tracker, **lock_kwargs):
            reg = _registry()
            for uid in REGISTRY_IDS:
                fn = reg[uid]
                try:
                    outcomes.append(fn(tracker))  # type: ignore[operator]
                except Exception as exc:  # noqa: BLE001 — skip-and-continue contract
                    logger.warning("ensure unit %s failed: %s", uid, exc)
                    outcomes.append(EnsureOutcome(uid, "failed", str(exc)))
            _write_applied(tracker, [o.id for o in outcomes if o.status != "failed"])
    except _lock.LockTimeout as exc:
        logger.warning("run_ensures: write lock unavailable, skipping sweep: %s", exc)
    except StoreIncompatibleError:
        # Preserve the compatibility gate across the broad recovery handler below.
        # Callers must reject a store this build cannot interpret (story 21dd).
        raise
    except Exception as exc:  # noqa: BLE001 — an ensure sweep must never abort its caller
        logger.warning("run_ensures: unexpected error, skipping sweep: %s", exc)
    return outcomes


# A covered write computes pending units once per process and store. When any remain, it emits
# one rate-limited warning that names them and recommends ``rebar fsck --repair``. Failures never
# block the write. A converged store performs no later marker reads.

# Cache of pending id sets, keyed by canonical tracker path (registry is static per
# process, so `.ensure-applied` is read at most once per store per process).
_pending_cache: dict[str, frozenset[str]] = {}


def _reset_pending_cache() -> None:
    """Clear the per-process pending cache (test hook; also lets a fresh sweep's
    result be re-observed within one process)."""
    _pending_cache.clear()


def _pending_ids(tracker: str) -> frozenset[str]:
    """The registry ids NOT yet in ``.ensure-applied`` for *tracker*, computed once
    per process per store and cached (so a converged store adds ≤1 marker read)."""
    key = _lock.canonical_tracker(tracker)
    cached = _pending_cache.get(key)
    if cached is None:
        cached = registry_ids() - applied_ids(key)
        _pending_cache[key] = cached
    return cached


def _read_hinted(tracker: str) -> float | None:
    """Parse ``.ensure-hinted`` → last-hinted unix timestamp, or ``None`` when the
    marker is absent/unparseable ("never hinted", symmetric with applied_ids)."""
    try:
        with open(os.path.join(tracker, HINTED_MARKER), encoding="utf-8") as fh:
            return float(fh.read().strip())
    except (OSError, ValueError):
        return None


def maybe_emit_pending_hint(tracker: str | os.PathLike) -> None:
    """Best-effort, fail-silent write-path nudge: if this store has pending ensure
    units and the last hint is older than the configured interval, log ONE WARNING
    naming the pending units and pointing at ``rebar fsck --repair``, then stamp
    ``.ensure-hinted``. Swallows ALL of its own exceptions (incl. lazy-import
    failures) so a committed write never fails because of it."""
    try:
        pending = _pending_ids(str(tracker))
        if not pending:
            return
        from rebar import config as _config

        cfg = _config.compose_config().ensure
        if not cfg.hint_enabled:
            return
        real = _lock.canonical_tracker(tracker)
        last = _read_hinted(real)
        now = time.time()
        if last is not None and (now - last) < cfg.hint_interval_secs:
            return
        try:
            fsutil.atomic_write(os.path.join(real, HINTED_MARKER), f"{now:.0f}\n")
        except OSError:
            pass  # best-effort stamp — still surface the hint
        logger.warning(
            "rebar: %d ensure unit(s) pending (%s) — run `rebar fsck --repair` to converge",
            len(pending),
            ", ".join(sorted(pending)),
        )
    except Exception:
        logger.debug("pending-hint suppressed", exc_info=True)
