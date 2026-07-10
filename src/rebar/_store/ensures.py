"""Idempotent per-environment ensure-registry (School B: desired-state convergence).

rebar's init-time ``_migrate_*``/``_ensure_*`` steps historically ran only at
``init``/re-init, so a config fix shipped *after* a store was initialized never
reached that store unless someone re-inited (the ``gc.auto=0`` legacy-store gap).
This module makes those steps first-class **ensure units**: check-then-act,
safe-to-re-run, drift-correcting (Ansible/Puppet ``changed``/``ok``; K8s
level-triggered reconcile) — *not* an ordered version ledger (that A-tier ledger
is future work; see ``docs/migrations.md``).

Each unit has a **stable, immutable id** and a ``callable(tracker) -> EnsureOutcome``
that is a no-op when already converged. :func:`run_ensures` runs ALL units
unconditionally under the store write lock (concurrent sweeps serialize; a second
sweep on a converged store makes zero git commits), catches a raising unit
(skip-and-continue → ``failed``), and rewrites the git-ignored ``.ensure-applied``
marker with the ids of the NON-failed (``ok``/``changed``) units via an atomic
temp+rename. The marker is a *hint* for the write-path pending-nudge and the
``rebar fsck`` ``ensures: N/M applied`` line — it NEVER gates whether a unit runs
(units are always re-run and self-check).

The unit *implementations* live in :mod:`rebar._commands.init` (they own the
``.gitignore``/``.gitattributes`` content constants); :func:`run_ensures`
lazy-imports them so the hot-path helpers here (:func:`registry_ids`,
:func:`applied_ids`) never pull ``init`` into a write path.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Literal

from rebar._store import fsutil
from rebar._store.lock import LockTimeout, canonical_tracker, write_lock

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


# The STABLE, IMMUTABLE id set — persisted in ``.ensure-applied`` and consulted by
# the pending-hint (WS2) + the ``fsck`` N/M line (WS3) WITHOUT importing ``init``.
# Renaming an id silently re-pends every store, so ``_registry()`` is asserted to
# cover exactly this set by ``tests`` (registry-drift guard).
REGISTRY_IDS: tuple[str, ...] = (
    "env-id",
    "gc-config",
    "merge-ours",
    "gitattributes",
    "gitignore",
)


def registry_ids() -> frozenset[str]:
    """The frozen set of registered ensure-unit ids (no ``init`` import — safe on
    the write hot-path and the read-only ``fsck`` line)."""
    return frozenset(REGISTRY_IDS)


def _registry() -> dict[str, object]:
    """Map id → check-then-act callable. Lazy-imports the unit implementations from
    :mod:`rebar._commands.init` (cold path — only :func:`run_ensures` calls this)."""
    from rebar._commands import init

    return {
        "env-id": init._ensure_env_id_unit,
        "gc-config": init._gc_config_unit,
        "merge-ours": init._merge_ours_unit,
        "gitattributes": init._gitattributes_unit,
        "gitignore": init._gitignore_unit,
    }


def _applied_path(tracker: str) -> str:
    return os.path.join(tracker, APPLIED_MARKER)


def applied_ids(tracker: str | os.PathLike) -> set[str]:
    """Parse ``.ensure-applied`` → set of applied unit ids. Absent/garbage/any
    non-list JSON degrades to the EMPTY set (never raises) — a pre-feature or
    corrupt marker simply reads as 'everything pending'."""
    try:
        raw = open(_applied_path(canonical_tracker(tracker)), encoding="utf-8").read()
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
    except OSError as exc:  # noqa: BLE001 handled — marker is a hint, not correctness
        logger.warning("run_ensures: could not write %s: %s", APPLIED_MARKER, exc)


def run_ensures(
    tracker: str | os.PathLike,
    *,
    timeout: int | None = None,
    attempts: int | None = None,
) -> list[EnsureOutcome]:
    """Run EVERY ensure unit unconditionally under the store write lock and rewrite
    ``.ensure-applied`` with the non-failed ids. Returns the per-unit outcomes.

    Never raises: a unit that raises is caught (skip-and-continue → ``failed`` and
    excluded from the marker); a write-lock acquisition failure is logged and
    treated like a whole-sweep no-op (init/boot never abort on ensure trouble).

    ``timeout``/``attempts`` bound the write-lock acquisition; ``None`` keeps
    ``write_lock``'s defaults. A caller that must not block (e.g. MCP boot) passes a
    SHORT budget so a contended lock skips the sweep rather than delaying it.
    """
    tracker = canonical_tracker(tracker)
    lock_kwargs: dict[str, Any] = {}
    if timeout is not None:
        lock_kwargs["timeout"] = timeout
    if attempts is not None:
        lock_kwargs["attempts"] = attempts
    outcomes: list[EnsureOutcome] = []
    try:
        with write_lock(tracker, **lock_kwargs):
            reg = _registry()
            for uid in REGISTRY_IDS:
                fn = reg[uid]
                try:
                    outcomes.append(fn(tracker))  # type: ignore[operator]
                except Exception as exc:  # noqa: BLE001 — skip-and-continue contract
                    logger.warning("ensure unit %s failed: %s", uid, exc)
                    outcomes.append(EnsureOutcome(uid, "failed", str(exc)))
            _write_applied(tracker, [o.id for o in outcomes if o.status != "failed"])
    except LockTimeout as exc:
        logger.warning("run_ensures: write lock unavailable, skipping sweep: %s", exc)
    except Exception as exc:  # noqa: BLE001 — an ensure sweep must never abort its caller
        logger.warning("run_ensures: unexpected error, skipping sweep: %s", exc)
    return outcomes


# ── WS2: write-path pending-hint (Rails CheckPending, hardened) ──────────────
#
# An existing store must learn it is behind the registry WITHOUT hot-path cost. On
# a covered write (see event_append.write_and_push) we compute the pending unit set
# ONCE per process per store (cached below), and — only when something is pending —
# emit a single, rate-limited WARNING that names the pending units and points at
# `rebar fsck --repair`. It is best-effort and fail-silent: a write must NEVER fail
# because of it. A converged store caches the empty set and does zero further reads.

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
    key = canonical_tracker(tracker)
    cached = _pending_cache.get(key)
    if cached is None:
        cached = registry_ids() - applied_ids(key)
        _pending_cache[key] = cached
    return cached


def _read_hinted(tracker: str) -> float | None:
    """Parse ``.ensure-hinted`` → last-hinted unix timestamp, or ``None`` when the
    marker is absent/unparseable ("never hinted", symmetric with applied_ids)."""
    try:
        return float(open(os.path.join(tracker, HINTED_MARKER), encoding="utf-8").read().strip())
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

        cfg = _config.load_config().ensure
        if not cfg.hint_enabled:
            return
        real = canonical_tracker(tracker)
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
    except Exception:  # noqa: BLE001 — fail-silent: the hint must NEVER fail a write
        logger.debug("pending-hint suppressed", exc_info=True)
