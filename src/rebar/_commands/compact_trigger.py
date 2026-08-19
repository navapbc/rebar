"""The OPERATION-LINKED compaction trigger — the floor that works without CI or cron.

Compaction left the close path (bug ``choosy-arthrodic-barbet``) because
``_compact_locked`` held the ONE store write lock for the whole fold: measured at 13m53s on a
single close, starving every concurrent writer. The scheduled sweep
(``.github/workflows/compact-sweep.yml``) replaced it as the amortized trigger — but a
schedule needs CI or cron, and an adopter running rebar as a library or CLI has neither. For
them a CI-only trigger is no trigger: their event logs would grow without bound and nothing in
the product would ever fold them. **Compaction has to work in environments without local cron
and CI. That is why the original design was linked to an operation.**

So the operation-linked trigger comes back — without the hold that made it a P0. The P0 was
never "compaction ran on close"; it was that an operation an agent WAITS ON held the store lock
for minutes. Here the waited-on operation holds nothing:

* the trigger runs at the very END of the close, AFTER ``txn.transition_core`` released the
  store lock and after the best-effort push;
* its decision is two ``O(1)`` checks — no store-wide scan, nothing that grows with the number
  of tickets;
* the fold itself happens in a DETACHED worker, so the session returns immediately. The
  worker's lock profile is exactly that of a manual ``rebar compact``, which is the accepted
  baseline, and it never spans an LLM call.

The detach machinery, the stamped advisory lock and the Windows no-op are all REUSED from
:mod:`rebar.llm.enrich_drain`, which already solved this exact shape for enrichment. Reusing it
(rather than writing a third dialect) is what lets this inherit the stamped-lock discipline
fixed in ``knavish-stimulated-bluebottle`` — pid-recycle qualification, refusal-without-proof
and the wall-clock ceiling — instead of re-deriving a heuristic that gets one of them wrong.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

#: Beside ``enrich-drain.lock`` in the repo's ``.rebar/`` — one compactor at a time.
_TRIGGER_LOCK_NAME = "compact-worker.lock"
#: Records when a sweep last ran, so the trigger can fire on staleness alone.
_SWEEP_STAMP_NAME = "compact-sweep.stamp"
_TRIGGER_LOG = "logs/compact-worker.log"


def _rebar_dir(tracker: str) -> str:
    """The repo's ``.rebar/`` (sibling of the ``.tickets-tracker`` dir), as the drain uses."""
    return os.path.join(os.path.dirname(tracker), ".rebar")


def _trigger_lock_path(tracker: str) -> str:
    return os.path.join(_rebar_dir(tracker), _TRIGGER_LOCK_NAME)


def _sweep_stamp_path(tracker: str) -> str:
    return os.path.join(_rebar_dir(tracker), _SWEEP_STAMP_NAME)


def _trigger_log_path(tracker: str) -> str:
    return os.path.join(_rebar_dir(tracker), _TRIGGER_LOG)


def record_sweep(tracker: str) -> None:
    """Stamp "a sweep ran just now". Best-effort: a missing stamp only makes the staleness
    check fire sooner, which costs one no-op sweep, never correctness."""
    try:
        os.makedirs(_rebar_dir(tracker), exist_ok=True)
        with open(_sweep_stamp_path(tracker), "w", encoding="utf-8") as fh:
            fh.write(str(int(time.time())))
    except OSError:
        logger.debug("could not write the compaction sweep stamp; continuing", exc_info=True)


def _sweep_is_stale(tracker: str, interval_s: int) -> bool:
    """Whether the last sweep is older than *interval_s* (one ``stat``).

    A MISSING stamp reads as stale: a store that has never swept is exactly the one that most
    needs to. ``interval_s <= 0`` disables the staleness arm entirely."""
    if interval_s <= 0:
        return False
    try:
        age = time.time() - os.stat(_sweep_stamp_path(tracker)).st_mtime
    except OSError:
        return True
    return age >= interval_s


def ticket_needs_folding(tracker: str, ticket_id: str) -> bool:
    """Whether THIS ticket alone satisfies the sweep's two-arm selection.

    ``O(1)`` in the size of the store — it reads one ticket directory, never scans the
    tracker — which is what makes it safe to run on the tail of every close. The store-wide
    question is the detached worker's job, not the session's.

    Deliberately asks the sweep's OWN question through the sweep's OWN helpers, so a change to
    what "needs folding" means cannot leave the trigger firing on a different rule than the
    thing it triggers."""
    from rebar import config as _config
    from rebar._commands.compact import _foldable_event_count
    from rebar._store import hlc

    ticket_dir = os.path.join(tracker, ticket_id)
    if not os.path.isdir(ticket_dir):
        return False
    try:
        cfg = _config.compose_config(os.path.dirname(tracker)).compact
        threshold, horizon = cfg.threshold, cfg.COMPACTION_HORIZON_NS
    except Exception:  # noqa: BLE001 — an unreadable config must never fail a close
        return False
    foldable = _foldable_event_count(ticket_dir, hlc.physical_now(), horizon)
    if foldable <= 0:
        return False
    try:
        has_snapshot = any(n.endswith("-SNAPSHOT.json") for n in os.listdir(ticket_dir))
    except OSError:
        return False
    return foldable > threshold or not has_snapshot


def _acquire_trigger_lock(tracker: str) -> int | None:
    """Best-effort non-blocking advisory lock: an open fd, or ``None`` if a live compactor
    holds it. NOT the store write lock — this only stops two worker PROCESSES doing redundant
    work, and a caller that cannot get it simply does not spawn.

    Carries the SAME v2 ownership stamp the store's mkdir lock writes, and a collision is
    adjudicated by the SAME shared decision table
    (:func:`rebar._store.lock_owner.stamped_file_is_stale`) the drain lock uses, so the
    pid-recycle qualification, the refuse-without-proof branches and the wall-clock ceiling are
    inherited rather than re-invented. Without that, a worker that died between acquire and
    release would leak this file and silently disable the trigger forever — which is precisely
    the bug that had to be fixed for the drain lock.

    A provably-orphaned lock is reclaimed LOUDLY and the create retried EXACTLY ONCE; a second
    collision means another worker won the race, so we give up rather than loop."""
    from rebar._store import lock_owner as _owner

    try:
        os.makedirs(_rebar_dir(tracker), exist_ok=True)
    except OSError:
        return None
    path = _trigger_lock_path(tracker)
    for attempt in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if attempt or not _owner.stamped_file_is_stale(path):
                return None
            logger.warning("compaction trigger: reclaiming stale worker lock %s", path)
            try:
                os.unlink(path)
            except OSError:
                return None  # someone else got there first; let them compact
            continue
        except OSError:
            return None
        try:
            os.write(fd, _owner._owner_stamp().encode("utf-8"))
        except OSError:
            pass  # a failed stamp only forfeits early reclamation of our own lock
        return fd
    return None


def release_trigger_lock(tracker: str, fd: int) -> None:
    """Drop the advisory lock (close the fd, unlink the file). Best-effort in both legs: a
    failed unlink leaves a stamped file that the shared staleness table will reclaim."""
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(_trigger_lock_path(tracker))
    except OSError:
        pass


def _detach_kwargs() -> dict:
    """Platform detach flags, mirroring :func:`rebar.llm.enrich_drain._detach_kwargs`."""
    if sys.platform == "win32":  # pragma: no cover - POSIX CI
        return {
            "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
        }
    return {"start_new_session": True, "close_fds": True}


def _spawn_detached_sweep(tracker: str) -> None:
    """Detach a sweep child that outlives this command (POSIX).

    Mirrors ``enrich_drain._spawn_detached_drain``: the same PYTHONPATH bootstrap so a bare
    python child can import rebar, the same stdio discipline (no stdin, output to a log), and
    the same never-raise posture — a detach failure must not fail the close that triggered it.
    """
    src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    child_env = {**os.environ}
    child_env["PYTHONPATH"] = src + (
        os.pathsep + child_env["PYTHONPATH"] if child_env.get("PYTHONPATH") else ""
    )
    try:
        os.makedirs(os.path.dirname(_trigger_log_path(tracker)), exist_ok=True)
        log_fh = open(_trigger_log_path(tracker), "a")  # noqa: SIM115 — handed to the child
    except OSError:
        log_fh = subprocess.DEVNULL  # type: ignore[assignment]
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, sys.argv[2]); "
                "from rebar._commands import compact_trigger; "
                "compact_trigger.run_sweep(sys.argv[1])",
                tracker,
                src,
            ],
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
            **_detach_kwargs(),
        )
    except Exception:
        logger.warning("compaction sweep detach failed; continuing", exc_info=True)


def run_sweep(tracker: str) -> None:
    """Hold the advisory lock and run the store-wide sweep. The detached child's entry point.

    Runs the SAME ``compact-all`` the scheduled workflow runs, so the two triggers cannot fold
    by different rules.

    Does NOT stamp the sweep itself: :func:`compact.compact_all_cli` stamps when it actually
    sweeps, and that is the only event the stamp may record. Stamping here — in a ``finally``,
    whatever happened — put a hole straight through the floor this trigger exists to be. A
    stand-aside folds nothing, so stamping it would reset the last-sweep clock and suppress the
    staleness arm for a full interval; under sustained contention every trigger would stand
    aside, stamp, and go quiet, and the store would never compact while looking freshly swept.
    A stand-aside must leave the clock alone so the next close tries again."""
    import contextlib
    import io

    fd = _acquire_trigger_lock(tracker)
    if fd is None:
        logger.debug("compaction trigger: a worker already holds the lock; skipping")
        return
    try:
        from rebar._commands import compact as _compact
        from rebar._store import lock as _lock

        # STAND ASIDE when a foreground writer holds the store lock. Compaction is optional
        # housekeeping and must yield to work someone is waiting on, never compete with it.
        #
        # This is the bug-7084 probe, and it is worth saying why it EARNS its place here after
        # being useless where it was born. Inline on the close path it could never fire: the
        # closing process had released the store lock microseconds earlier, so its own probe
        # always read "free" and compaction took the lock regardless. Out of band the probe
        # measures something real — another SESSION's activity — because the compactor is a
        # different process from every writer it might obstruct. Moving the work is what made
        # the guard meaningful.
        #
        # Racy by nature and fail-open by design: a writer can arrive one microsecond later, and
        # a skipped sweep is a no-op for correctness (the events stay live and the next trigger
        # folds them), so the cost of guessing wrong in either direction is nil.
        if _lock.write_lock_is_busy(tracker):
            logger.info(
                "compaction sweep standing aside: store write lock busy (holder: %s); "
                "the events stay live and the next trigger folds them",
                _lock.describe_lock_holder(tracker),
            )
            return
        repo_root = os.path.dirname(tracker)
        # Tag the store-lock acquisitions this sweep drives (transitively, via
        # compact_all_cli -> _compact_locked) as `op=compact-sweep`, so a blocked writer's
        # LockTimeout names the sweep and its safe-to-interrupt remedy rather than a bare pid
        # (camerashy-erectable-frog). The label is ambient — it reaches `_owner_stamp` through
        # the contextvar, not by editing the acquisition call site.
        from rebar._store import lock_owner as _owner

        with contextlib.redirect_stdout(io.StringIO()), _owner.operation_label("compact-sweep"):
            _compact.compact_all_cli([], repo_root=repo_root)
    except Exception:
        logger.warning("compaction sweep failed; the events stay live", exc_info=True)
    finally:
        release_trigger_lock(tracker, fd)


def maybe_compact(tracker: str, ticket_id: str, *, repo_root=None) -> None:
    """The operation-linked trigger, called on the tail of a close.

    Fires when the ticket just written needs folding, OR when the last sweep is stale — the
    second arm is what makes this a real floor rather than a coincidence, since a store whose
    closed tickets happen not to be foldable would otherwise never fold the ones that are.

    NEVER raises and never blocks: every branch is guarded, and the default path hands the work
    to a detached child. Windows is a v1 no-op, mirroring the enrichment drain, because the
    store lock's ``fcntl`` import would crash a detached child there."""
    try:
        if sys.platform == "win32":  # pragma: no cover - POSIX CI
            return
        from rebar import config as _config

        try:
            cfg = _config.compose_config(
                repo_root if repo_root is not None else os.path.dirname(tracker)
            ).compact
        except Exception:  # noqa: BLE001 — an unreadable config must never fail a close
            return
        if cfg.trigger == "off":
            return
        if not (
            ticket_needs_folding(tracker, ticket_id)
            or _sweep_is_stale(tracker, cfg.trigger_interval_s)
        ):
            return
        if cfg.trigger == "always":
            run_sweep(tracker)  # synchronous inline (tests/CI)
            return
        # Don't detach a worker when one is already running: the lock is the storm control, and
        # probing it here keeps a burst of closes from spawning a burst of children. The child
        # re-acquires it for real, so losing this race is harmless.
        probe = _acquire_trigger_lock(tracker)
        if probe is None:
            return
        release_trigger_lock(tracker, probe)
        _spawn_detached_sweep(tracker)
    except Exception:
        logger.warning("compaction trigger failed; continuing", exc_info=True)
