"""Portable, operation-linked snapshot reclamation outside the review-bot server.

CLI, CI, and library gate resolution can populate the per-host store without a resident
janitor, so every attested gate cheaply considers GC. The decision performs one stamp
``stat``—never a store walk or ticket-store lock—and runs any pass in a detached child.
A v2 stamped worker lock supplies cross-process single-flight, PID-reuse handling,
fail-without-proof behavior, and a wall-clock ceiling. Sidecars live in the canonical
``<store>/gc`` shared by all worktrees.

This driver and the resident janitor both enter :func:`janitor.run_gc`; its non-blocking
store lock makes overlap harmless. Only a pass that actually ran updates the stamp, so a
contended stand-aside does not suppress the next attempt.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from rebar._store.stamped_lock import release_stamped_lock, stamped_file_lock

if TYPE_CHECKING:
    from rebar._snapshot.janitor import JanitorConfig

logger = logging.getLogger(__name__)

#: Records when a GC pass last actually RAN on this host's store.
_STAMP_NAME = "last-pass.stamp"
#: One detached GC worker at a time (spawn-storm control; ``run_gc``'s flock guards the pass).
_WORKER_LOCK_NAME = "worker.lock"
#: The detached child's stderr, beside the sidecars it belongs to.
_LOG_NAME = "worker.log"


#: The janitor's sidecar directory under the store root (it already hosts ``gc/lock``).
_GC_DIRNAME = "gc"


def _gc_dir(root: Path) -> Path:
    """``<store>/gc/`` — the janitor's own sidecar directory (it already hosts ``gc/lock``)."""
    d = root / _GC_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stamp_path(root: Path) -> Path:
    return _gc_dir(root) / _STAMP_NAME


def _worker_lock_path(root: Path) -> Path:
    return _gc_dir(root) / _WORKER_LOCK_NAME


def _log_path(root: Path) -> Path:
    return _gc_dir(root) / _LOG_NAME


def worker_lock_probe_path() -> Path:
    """Return this host's worker-lock path without creating the store.

    ``rebar doctor`` uses the owner-defined layout with ``peek_store_root`` so its census
    has no mkdir or chmod side effects."""
    from rebar._snapshot.repo_snapshot import peek_store_root

    return peek_store_root() / _GC_DIRNAME / _WORKER_LOCK_NAME


def record_pass(root: Path) -> None:
    """Stamp "a GC pass ran just now". Best-effort: a missing stamp only makes the next
    trigger fire sooner, which costs one no-op pass, never correctness."""
    try:
        with open(_stamp_path(root), "w", encoding="utf-8") as fh:
            fh.write(str(int(time.time())))
    except OSError:
        logger.debug("could not write the snapshot-GC stamp; continuing", exc_info=True)


def _pass_is_due(root: Path, interval_s: int) -> bool:
    """Check one stamp for an overdue pass; a missing stamp is due.

    Nonpositive ``interval_s`` disables the trigger through the janitor's existing knob."""
    if interval_s <= 0:
        return False
    try:
        age = time.time() - os.stat(_stamp_path(root)).st_mtime
    except OSError:
        return True
    return age >= interval_s


def _acquire_worker_lock(root: Path) -> int | None:
    """Take the shared non-blocking stamped worker lock, never a ticket-store lock.

    Return its fd, or ``None`` when the sidecar cannot be created or a worker is live."""
    try:
        path = _worker_lock_path(root)
    except OSError:
        return None
    return stamped_file_lock(path, label="snapshot-GC trigger")


def release_worker_lock(root: Path, fd: int) -> None:
    """Drop the advisory lock (close the fd, unlink the file), both legs best-effort."""
    release_stamped_lock(_worker_lock_path(root), fd)


def _janitor_config(repo_root: str | None) -> JanitorConfig:
    """The janitor tunables, degrading to documented defaults when *repo_root* is unreadable
    (a detached child can outlive the worktree whose config it was spawned from)."""
    from rebar._snapshot import janitor as _janitor

    try:
        return _janitor.JanitorConfig.from_env(repo_root)
    except Exception:  # noqa: BLE001 — an unreadable config must never fail housekeeping
        return _janitor.JanitorConfig()


def _spawn_detached_gc(root: Path, repo_root: str | None) -> None:
    """Spawn GC detached from the gate, using the durable store root as ``cwd``.

    The shared spawner owns bootstrap, re-entry, platform flags, and stdio. Failures never
    fail the gate; an absent ``repo_root`` crosses argv as ``""`` for :func:`run_detached`."""
    from rebar._proc import spawn_detached

    try:
        log_fh = open(_log_path(root), "a")  # noqa: SIM115 — handed to the child
    except OSError:
        log_fh = subprocess.DEVNULL  # type: ignore[assignment]
    try:
        spawn_detached(
            "rebar._snapshot.gc_trigger",
            "run_detached",
            str(root),
            repo_root or "",
            env={**os.environ},
            stderr=log_fh,
        )
    except Exception:
        logger.warning("snapshot-GC detach failed; continuing", exc_info=True)


def run_detached(root: str | os.PathLike[str], repo_root: str | None = None) -> None:
    """Run the resident janitor's policy under the detached worker lock.

    :class:`~rebar._snapshot.janitor.JanitorConfig` carries watermark, grace, cold-trim,
    byte-cap, and entry-cap policy into :func:`janitor.run_gc`; the trigger itself remains
    one ``stat``. The GC lock resolves overlap with the resident driver. Stamp only a pass
    that ran, never a lock-contention skip."""
    # The shared spawner's argv carries plain strings; the detached stub hands "" through
    # for an absent repo root, so coerce it back to None here (the child's entry point).
    repo_root = repo_root or None
    rootp = Path(root)
    fd = _acquire_worker_lock(rootp)
    if fd is None:
        logger.debug("snapshot-GC trigger: a worker already holds the lock; skipping")
        return
    try:
        from rebar._snapshot import janitor as _janitor

        res = _janitor.run_gc(rootp, config=_janitor_config(repo_root))
        if res.skipped is None:
            record_pass(rootp)
    except Exception:
        logger.warning("snapshot-GC pass failed; the entries stay live", exc_info=True)
    finally:
        release_worker_lock(rootp, fd)


def maybe_gc(repo_root: str | None = None) -> None:
    """Consider detached GC after an attested gate without raising or blocking it.

    The trigger resolves existing janitor settings, stats one stamp, holds no ticket-store
    lock, and never enumerates the store. Windows remains a no-op."""
    try:
        if sys.platform == "win32":  # pragma: no cover - POSIX CI
            return
        cfg = _janitor_config(repo_root)
        if cfg.interval_seconds <= 0:
            return
        from rebar._snapshot.repo_snapshot import store_root

        root = store_root()
        if not _pass_is_due(root, cfg.interval_seconds):
            return
        # Probe the worker lock to prevent spawn storms; the child reacquires it, so a race
        # only creates a harmless contender.
        probe = _acquire_worker_lock(root)
        if probe is None:
            return
        release_worker_lock(root, probe)
        _spawn_detached_gc(root, repo_root)
    except Exception:
        logger.warning("snapshot-GC trigger failed; continuing", exc_info=True)
