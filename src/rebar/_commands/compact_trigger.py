"""Operation-linked compaction trigger for environments without cron or CI.
After close releases the store lock, make bounded eligibility checks and
start a detached sweep worker. Use shared process and stamped-lock helpers.
The waited-on close does not hold the lock through a fold."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

from rebar._store.paths import StorePaths
from rebar._store.stamped_lock import release_stamped_lock, stamped_file_lock

logger = logging.getLogger(__name__)

#: In the repo's ``.rebar/`` beside the clock lock — one compactor at a time.
_TRIGGER_LOCK_NAME = "compact-worker.lock"
#: Records when a sweep last ran, so the trigger can fire on staleness alone.
_SWEEP_STAMP_NAME = "compact-sweep.stamp"
_TRIGGER_LOG = "logs/compact-worker.log"


def _trigger_lock_path(tracker: str) -> str:
    return StorePaths(tracker).sidecar(_TRIGGER_LOCK_NAME)


def _sweep_stamp_path(tracker: str) -> str:
    return StorePaths(tracker).sidecar(_SWEEP_STAMP_NAME)


def _trigger_log_path(tracker: str) -> str:
    return StorePaths(tracker).log(_TRIGGER_LOG)


def record_sweep(tracker: str) -> None:
    """Stamp "a sweep ran just now". Best-effort: a missing stamp only makes the staleness
    check fire sooner, which costs one no-op sweep, never correctness."""
    try:
        os.makedirs(StorePaths(tracker).rebar_dir, exist_ok=True)
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


def ticket_needs_folding(tracker: str, ticket_id: str, *, repo_root=None) -> bool:
    """Whether THIS ticket alone satisfies the sweep's two-arm selection.

    ``O(1)`` in the size of the store — it reads one ticket directory, never scans the
    tracker — which is what makes it safe to run on the tail of every close. The store-wide
    question is the detached worker's job, not the session's.

    Deliberately asks the sweep's OWN question through the sweep's OWN helpers, so a change to
    what "needs folding" means cannot leave the trigger firing on a different rule than the
    thing it triggers."""
    from rebar import config as _config
    from rebar._commands import compact_plan
    from rebar._store import hlc

    ticket_dir = os.path.join(tracker, ticket_id)
    if not os.path.isdir(ticket_dir):
        return False
    try:
        # Config read: resolve from repo_root (None == discover), NOT os.path.dirname(tracker),
        # which is the repo root only for a co-located store (auspicial-friended-merganser).
        cfg = _config.compose_config(repo_root).compact
        threshold, horizon = cfg.threshold, cfg.COMPACTION_HORIZON_NS
        snapshot_alpha = cfg.snapshot_alpha
    except Exception:  # noqa: BLE001 — an unreadable config must never fail a close
        return False
    now = hlc.physical_now()
    stats = compact_plan.foldable_stats(ticket_dir, now, horizon)
    if stats.count <= 0:
        return False
    has_snapshot = compact_plan.has_snapshot(ticket_dir)
    if has_snapshot is None:
        return False  # unreadable ticket dir: a close must never fire on a guess
    return compact_plan.needs_folding(
        stats.count,
        has_snapshot,
        threshold,
        snapshot_alpha=snapshot_alpha,
        pending_source_bytes=stats.pending_source_bytes,
        active_snapshot_bytes=stats.active_snapshot_bytes,
    )


def _acquire_trigger_lock(tracker: str) -> int | None:
    """Best-effort non-blocking advisory lock: an open fd, or ``None`` if a live compactor holds
    it. NOT the store write lock — a caller that cannot get it simply does not spawn. The
    mechanism is :func:`rebar._store.stamped_lock.stamped_file_lock`, shared with the drain and
    snapshot-GC triggers; only the sidecar directory is this site's own."""
    try:
        os.makedirs(StorePaths(tracker).rebar_dir, exist_ok=True)
    except OSError:
        return None
    return stamped_file_lock(_trigger_lock_path(tracker), label="compaction trigger")


def release_trigger_lock(tracker: str, fd: int) -> None:
    """Drop the advisory lock (close the fd, unlink the file), both legs best-effort."""
    release_stamped_lock(_trigger_lock_path(tracker), fd)


def _spawn_detached_sweep(tracker: str) -> None:
    """Detach a sweep child that outlives this command (POSIX), via the shared
    detached-rebar-child spawner (:func:`rebar._proc.spawn_detached`), which owns the
    PYTHONPATH bootstrap, the ``-c`` re-entry stub, the detach flags and the durable-cwd
    anchor. This site keeps its own stderr sink (the store-scoped worker log), its canonical
    tracker argument, and its broad never-raise catch — a detach failure must not fail the
    close that triggered it."""
    from rebar._proc import spawn_detached

    try:
        os.makedirs(os.path.dirname(_trigger_log_path(tracker)), exist_ok=True)
        log_fh = open(_trigger_log_path(tracker), "a")  # noqa: SIM115 — handed to the child
    except OSError:
        log_fh = subprocess.DEVNULL  # type: ignore[assignment]
    try:
        spawn_detached(
            "rebar._commands.compact_trigger",
            "run_detached_sweep",
            # The canonical store tracker, for the same reason spawn_detached resolves the
            # child's cwd: the child outlives the worktree that spawned it, so handing over a
            # worktree's SYMLINK would leave the running child pointed at a path that dies
            # with the worktree (bug 93a9-66cf-e681-4f49).
            StorePaths(tracker).canonical,
            env={**os.environ},
            stderr=log_fh,
        )
    except Exception:
        logger.warning("compaction sweep detach failed; continuing", exc_info=True)


def install_stop_on_sigterm() -> None:
    """Make SIGTERM end this process's sweep at its next ticket boundary.

    The detached worker runs in its own session, so ``docker stop`` never reaches it; the
    retiring MCP server forwards SIGTERM through :func:`stop_local_sweep` instead (bug
    topnotch-brazen-slug). The handler only sets the sweep's stop flag, so the fold in flight
    finishes and the end-of-run push still runs. Main thread only, as :mod:`signal` requires."""
    import signal

    from rebar._commands import compact as _compact

    def _on_sigterm(_signum: int, _frame: object) -> None:
        _compact.request_stop()

    signal.signal(signal.SIGTERM, _on_sigterm)


def run_detached_sweep(tracker: str) -> None:
    """The detached worker's entrypoint: :func:`run_sweep` with SIGTERM as a stop request.

    Kept apart from :func:`run_sweep` because the inline ``always`` trigger runs that inside
    a host process whose own SIGTERM handling must not be replaced."""
    install_stop_on_sigterm()
    run_sweep(tracker)


def _read_trigger_stamp(tracker: str) -> str | None:
    try:
        with open(_trigger_lock_path(tracker), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def stop_local_sweep(tracker: str) -> int | None:
    """Send SIGTERM to a sweep worker running in THIS PID namespace; return its pid.

    The worker is found through its worker-lock stamp. The tickets clone is shared by every
    MCP container on the host, so the stamp may name another container's worker; only a pid
    the stamp proves live in our own namespace is signalled (``unverified-live`` is accepted
    only where the platform cannot read start times, i.e. has no ``/proc``). Returns ``None``
    when there is nothing of ours to stop. Never raises."""
    import signal

    from rebar._store import lock_owner as _owner

    try:
        stamp = _read_trigger_stamp(tracker)
        fields = _owner._parse_v2_stamp(stamp) if stamp else None
        if not fields:
            return None
        verdict = _owner._describe_stamped_pid(fields)
        if not verdict.startswith(("live", "unverified-live")):
            logger.info("retiring: compaction sweep lock holder not ours (%s); left alone", verdict)
            return None
        pid = int(fields["pid"])
        if pid == os.getpid():
            return None
        os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        logger.warning("retiring: could not signal the compaction sweep", exc_info=True)
        return None
    logger.warning("retiring: asked compaction sweep pid %d to stop at its next ticket", pid)
    return pid


def _sweep_exited(tracker: str, pid: int) -> bool:
    """Whether worker *pid* has finished: reaped (it may be our child, which would otherwise
    linger as a zombie that signal 0 still finds), gone, or no longer the lock's holder."""
    from rebar._store import lock_owner as _owner

    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return True
    except OSError:
        pass  # not our child: init reaps it, and signal 0 below sees it go
    if not _owner._pid_alive(pid):
        return True
    stamp = _read_trigger_stamp(tracker)
    return stamp is None or f" pid={pid} " not in f"{stamp} "


def retire_local_sweep(
    tracker: str, *, poll_s: float = 0.2, sleep=time.sleep, monotonic=time.monotonic
):
    """Stop this namespace's sweep now; return ``finish(deadline)`` for the exit path.

    ``finish`` waits, never past *deadline* (a ``time.monotonic`` value), for the signalled
    worker to exit and then for the store write lock to be free, so an in-flight fold or
    event write completes before the process exits. Past the deadline it logs and returns:
    the fold's intent journal recovers a fold the container's final SIGKILL interrupts."""
    from rebar._store import lock as _lock

    pid = stop_local_sweep(tracker)

    def finish(deadline: float) -> None:
        while pid is not None and not _sweep_exited(tracker, pid) and monotonic() < deadline:
            sleep(poll_s)
        while _lock.write_lock_is_busy(tracker) and monotonic() < deadline:
            sleep(poll_s)
        if monotonic() >= deadline and _lock.write_lock_is_busy(tracker):
            logger.warning(
                "retiring: grace expired with the store write lock still held (%s); exiting",
                _lock.describe_lock_holder(tracker),
            )

    return finish


def run_sweep(tracker: str) -> None:
    """Run the same store-wide sweep as the scheduled workflow under its
    advisory worker lock. Let compact_all_cli stamp only a sweep that ran.
    A stand-aside leaves the clock unchanged so later closes retry."""
    import contextlib
    import io

    # The detached child is its own process entrypoint and its stderr IS the store's worker
    # log, but no handler was installed, so every record (warnings included) was dropped and
    # the production log stayed empty (bug breezelike-travelsick-moray). Idempotent, so an
    # inline ``always`` run inside an entrypoint that already installed one adds nothing.
    from rebar._logging import install_stderr_handler

    install_stderr_handler()
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
        # RESOLVE the code root; never compose it from the store path. `tracker` is the
        # STORE, and the store is RELOCATABLE (REBAR_TRACKER_DIR): the former
        # os.path.dirname(tracker) equalled the code root only for a co-located checkout, so
        # on the deployed topology (store /var/gerrit/site/mcp-tickets) the sweep read DEFAULT
        # compaction config and folded the right tickets by the WRONG rule
        # (scathing-custommade-bobcat). The bare resolver is correct for BOTH supported
        # topologies even though this child holds only `tracker`:
        #   * deployed: REBAR_ROOT is exported unconditionally and names the provisioned
        #     CHECKOUT at /var/gerrit/site/mcp-code — NOT the /app WORKDIR, which is a source
        #     copy whose .git is excluded by .dockerignore. Resolving is what keeps us off
        #     /app; composing from the store, or defaulting to the cwd, lands on a directory
        #     with no object DB (the fatherly-incoherent-mare shape).
        #   * local: the git-toplevel arm reads a cwd that `_proc.detached_child_cwd` has
        #     already anchored at the CANONICAL store's parent — the durable main checkout,
        #     deliberately not the ephemeral worktree that spawned us.
        # `None` (nothing resolved) is passed through as the discover sentinel, exactly as
        # maybe_compact's own compose_config call does.
        from rebar import config as _cfg

        repo_root = _cfg.repo_root_or_none()
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
    to a detached child. Windows is a v1 no-op, because the
    store lock's ``fcntl`` import would crash a detached child there."""
    try:
        if sys.platform == "win32":  # pragma: no cover - POSIX CI
            return
        from rebar import config as _config

        try:
            # None discovers the config root correctly; the former os.path.dirname(tracker)
            # fallback only found rebar.toml for a co-located store (auspicial-friended-merganser).
            cfg = _config.compose_config(repo_root).compact
        except Exception:  # noqa: BLE001 — an unreadable config must never fail a close
            return
        if cfg.trigger == "off":
            return
        if not (
            ticket_needs_folding(tracker, ticket_id, repo_root=repo_root)
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
