"""The OPERATION-LINKED snapshot-GC trigger — reclamation without the review-bot server.

:func:`janitor.run_gc` had exactly one production driver: the review-bot FastAPI lifespan's
resident thread (:func:`janitor.start_background_janitor`). Every OTHER host that resolves an
attested gate — a laptop running ``rebar review-plan``, a CI runner, any library embedding —
populates ``$REBAR_GATE_TMPDIR/rebar-gate-snapshots`` and never reclaims it; one developer host
measured 64,021 entries / 47.24 GiB, append-only for the life of the machine (bug
``undamaged-epidermic-kakarikis``). A trigger keyed to a CI provider or a daemon would not be
portable (``project.portability``), so — exactly as compaction concluded in
:mod:`rebar._commands.compact_trigger` — the floor has to be linked to an operation the host
already runs: gate resolution itself.

The operator ruling on that ticket pins the shape: an operation-linked trigger is acceptable
ONLY with the pattern that fixed bug ``0d15-59a4`` ("Sweep walks full git history per ticket
under the store lock"). Concretely:

* the trigger's decision is near-free — ONE ``stat`` of a stamp sidecar (the O(1) marker
  discipline the enrich-drain gate settled on), never an enumeration of a store that
  this bug measured at 64k entries;
* it takes NO ticket-store lock in any branch — nothing here imports ``rebar._store.lock``,
  and the pass it spawns operates on the snapshot store, which has its OWN interlocks
  (``run_gc``'s non-blocking flock on ``<store>/gc/lock``; the byte total's flock);
* the pass runs in a DETACHED child, so the gate that triggered it returns immediately and the
  child outlives the (possibly ephemeral) worktree that spawned it;
* single-flight across simultaneous hosts'-worktrees/processes via a worker-lock sidecar
  carrying the v2 owner stamp, adjudicated by the SHARED decision table
  (:func:`rebar._store.lock_owner.stamped_file_is_stale`) so pid-recycle qualification,
  refuse-without-proof and the wall-clock ceiling are inherited, not re-derived.

Unlike the compaction trigger's sidecars — keyed on the canonical TRACKER because worktrees
view one store through symlinks — these sidecars live inside the snapshot store itself
(``<store>/gc/``): the store is per-host and already canonical (``store_root()`` resolves it
identically from every worktree), so every checkout on the host shares one clock and one lock
by construction.

The review-bot's resident janitor is untouched and remains a supplementary cadence. Overlap is
harmless by construction: both drivers funnel into ``run_gc``, whose non-blocking flock makes
the loser return ``skipped="locked"`` — and a skipped pass does NOT stamp (the ``run_sweep``
lesson: a stand-aside that resets the clock goes quiet forever under contention).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rebar._snapshot.janitor import JanitorConfig

logger = logging.getLogger(__name__)

#: Records when a GC pass last actually RAN on this host's store.
_STAMP_NAME = "last-pass.stamp"
#: One detached GC worker at a time (spawn-storm control; ``run_gc``'s flock guards the pass).
_WORKER_LOCK_NAME = "worker.lock"
#: The detached child's stderr, beside the sidecars it belongs to.
_LOG_NAME = "worker.log"


def _gc_dir(root: Path) -> Path:
    """``<store>/gc/`` — the janitor's own sidecar directory (it already hosts ``gc/lock``)."""
    d = root / "gc"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stamp_path(root: Path) -> Path:
    return _gc_dir(root) / _STAMP_NAME


def _worker_lock_path(root: Path) -> Path:
    return _gc_dir(root) / _WORKER_LOCK_NAME


def _log_path(root: Path) -> Path:
    return _gc_dir(root) / _LOG_NAME


def record_pass(root: Path) -> None:
    """Stamp "a GC pass ran just now". Best-effort: a missing stamp only makes the next
    trigger fire sooner, which costs one no-op pass, never correctness."""
    try:
        with open(_stamp_path(root), "w", encoding="utf-8") as fh:
            fh.write(str(int(time.time())))
    except OSError:
        logger.debug("could not write the snapshot-GC stamp; continuing", exc_info=True)


def _pass_is_due(root: Path, interval_s: int) -> bool:
    """Whether the last pass is older than *interval_s* (one ``stat`` — the whole point).

    A MISSING stamp reads as due: the host that never reclaimed is exactly the one that most
    needs to. ``interval_s <= 0`` disables the trigger entirely (the off switch — the knob is
    the janitor's own ``interval_seconds`` cadence, not a new one)."""
    if interval_s <= 0:
        return False
    try:
        age = time.time() - os.stat(_stamp_path(root)).st_mtime
    except OSError:
        return True
    return age >= interval_s


def _acquire_worker_lock(root: Path) -> int | None:
    """Best-effort non-blocking advisory lock: an open fd, or ``None`` if a live worker holds
    it. NOT any ticket-store lock — this only stops two GC workers doing redundant work, and a
    caller that cannot get it simply does not spawn.

    Carries the SAME v2 ownership stamp the store's mkdir lock writes, and a collision is
    adjudicated by the SAME shared decision table
    (:func:`rebar._store.lock_owner.stamped_file_is_stale`) the drain and compaction worker
    locks use, so pid-recycle qualification, refuse-without-proof and the wall-clock ceiling
    are inherited rather than re-invented. A provably-orphaned lock is reclaimed LOUDLY and
    the create retried EXACTLY ONCE; a second collision means another worker won the race."""
    from rebar._store import lock_owner as _owner

    try:
        path = _worker_lock_path(root)
    except OSError:
        return None
    for attempt in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if attempt or not _owner.stamped_file_is_stale(str(path)):
                return None
            logger.warning("snapshot-GC trigger: reclaiming stale worker lock %s", path)
            try:
                os.unlink(path)
            except OSError:
                return None  # someone else got there first; let them collect
            continue
        except OSError:
            return None
        try:
            os.write(fd, _owner._owner_stamp().encode("utf-8"))
        except OSError:
            pass  # a failed stamp only forfeits early reclamation of our own lock
        return fd
    return None


def release_worker_lock(root: Path, fd: int) -> None:
    """Drop the advisory lock (close the fd, unlink the file). Best-effort in both legs: a
    failed unlink leaves a stamped file that the shared staleness table will reclaim."""
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(_worker_lock_path(root))
    except OSError:
        pass


def _janitor_config(repo_root: str | None) -> JanitorConfig:
    """The janitor tunables, degrading to documented defaults when *repo_root* is unreadable
    (a detached child can outlive the worktree whose config it was spawned from)."""
    from rebar._snapshot import janitor as _janitor

    try:
        return _janitor.JanitorConfig.from_env(repo_root)
    except Exception:  # noqa: BLE001 — an unreadable config must never fail housekeeping
        return _janitor.JanitorConfig()


def _detach_kwargs() -> dict:
    """Platform detach flags, mirroring :func:`compact_trigger._detach_kwargs`."""
    if sys.platform == "win32":  # pragma: no cover - POSIX CI
        return {
            "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
        }
    return {"start_new_session": True, "close_fds": True}


def _spawn_detached_gc(root: Path, repo_root: str | None) -> None:
    """Detach a GC child that outlives this gate op (POSIX).

    Mirrors ``compact_trigger._spawn_detached_sweep``: the same PYTHONPATH bootstrap so a bare
    python child can import rebar, the same stdio discipline (no stdin, stderr to a log), and
    the same never-raise posture — a detach failure must not fail the gate that triggered it.
    The child's ``cwd`` is the STORE ROOT itself: per-host, outside any repo, and never an
    ephemeral worktree that can vanish mid-pass (the bug ``3198-438c-72a5-470f`` concern
    :func:`rebar._proc.detached_child_cwd` exists for, satisfied here by construction)."""
    src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    child_env = {**os.environ}
    child_env["PYTHONPATH"] = src + (
        os.pathsep + child_env["PYTHONPATH"] if child_env.get("PYTHONPATH") else ""
    )
    try:
        log_fh = open(_log_path(root), "a")  # noqa: SIM115 — handed to the child
    except OSError:
        log_fh = subprocess.DEVNULL  # type: ignore[assignment]
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, sys.argv[3]); "
                "from rebar._snapshot import gc_trigger; "
                "gc_trigger.run_detached(sys.argv[1], sys.argv[2] or None)",
                str(root),
                repo_root or "",
                src,
            ],
            cwd=str(root),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
            **_detach_kwargs(),
        )
    except Exception:
        logger.warning("snapshot-GC detach failed; continuing", exc_info=True)


def run_detached(root: str | os.PathLike[str], repo_root: str | None = None) -> None:
    """Hold the worker lock and run one :func:`janitor.run_gc` pass. The child's entry point.

    Runs the SAME policy the review-bot's resident janitor runs (hysteretic watermark, grace
    window, cold-trim, ``max_bytes`` backstop), so the two drivers cannot reclaim by different
    rules. Overlap with that janitor is harmless: ``run_gc``'s non-blocking flock makes the
    loser return ``skipped="locked"``.

    Stamps ONLY a pass that actually ran: a stand-aside must leave the clock alone so the next
    gate op tries again — stamping it would suppress the trigger for a full interval while the
    store reclaimed nothing (the ``compact_trigger.run_sweep`` lesson, verbatim)."""
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
    """The operation-linked trigger, called on the tail of an attested gate resolution.

    NEVER raises and never blocks the gate: its decision is one stamp ``stat`` (plus the
    janitor-tunable resolution the gate has already paid for several times over), every branch
    is guarded, and the pass itself happens in a detached child. It holds NO ticket-store lock
    — this module never touches ``rebar._store.lock`` — and does not enumerate the store.
    Windows is a v1 no-op, mirroring the compaction trigger and the enrichment drain."""
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
        # Don't detach a worker when one is already running: the lock is the storm control,
        # and probing it here keeps a burst of gate ops from spawning a burst of children.
        # The child re-acquires it for real, so losing this race is harmless.
        probe = _acquire_worker_lock(root)
        if probe is None:
            return
        release_worker_lock(root, probe)
        _spawn_detached_gc(root, repo_root)
    except Exception:
        logger.warning("snapshot-GC trigger failed; continuing", exc_info=True)
