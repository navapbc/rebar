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
from dataclasses import dataclass
from typing import Literal

from rebar._store import fsutil
from rebar._store.lock import LockTimeout, canonical_tracker, write_lock

logger = logging.getLogger("rebar")

# Git-ignored marker (JSON array of non-failed unit ids). Absent/garbage → empty set.
APPLIED_MARKER = ".ensure-applied"

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


def run_ensures(tracker: str | os.PathLike) -> list[EnsureOutcome]:
    """Run EVERY ensure unit unconditionally under the store write lock and rewrite
    ``.ensure-applied`` with the non-failed ids. Returns the per-unit outcomes.

    Never raises: a unit that raises is caught (skip-and-continue → ``failed`` and
    excluded from the marker); a write-lock acquisition failure is logged and
    treated like a whole-sweep no-op (init/boot never abort on ensure trouble).
    """
    tracker = canonical_tracker(tracker)
    outcomes: list[EnsureOutcome] = []
    try:
        with write_lock(tracker):
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
