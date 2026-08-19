"""Tier-1 opportunistic detached enrichment drain + ``rebar enrich`` CLI backing
(epic only-crave-art, story c1de).

The overlap feature must run async across server/PC/Mac/cloud with ZERO client setup — no
daemon, broker, or scheduler. Following the git-gc-auto / npm-update-notifier pattern, a
cheap ``maybe_drain()`` gate on ordinary invocations no-ops in the common case, else DETACHES
the enrichment to a child that outlives the command (reusing push.py's POSIX detach). The
drainer loop claims soaked queue entries (optimistic, per S4), runs enrich (S1), writes the
digest (S2), and marks done — bounded per run, self-healing on crash.

**Windows drain is a documented v1 NO-OP:** the store write lock (``_store/lock.py``) imports
``fcntl`` unconditionally, so a detached drain child would crash at import on Windows; rather
than spawn a doomed child, ``maybe_drain`` returns early on ``os.name == "nt"``. The Windows
``creationflags`` detach branch is authored (API-derisked) but not reached in v1.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

_DRAIN_LOCK_NAME = "enrich-drain.lock"
_DRAIN_LOG = "logs/enrich-drain.log"
# A stable per-process drainer id (varies by pid so distinct drain processes are distinct
# claimants). Time-independent enough for arbitration (pid + a monotonic counter suffix).
_DRAINER_SEQ = 0


def _drainer_id() -> str:
    global _DRAINER_SEQ
    _DRAINER_SEQ += 1
    return f"drainer-{os.getpid()}-{_DRAINER_SEQ}"


def _rebar_dir(tracker: str) -> str:
    # The tracker is .../.tickets-tracker; the repo's .rebar dir is its sibling under the repo.
    return os.path.join(os.path.dirname(tracker), ".rebar")


def _drain_lock_path(tracker: str) -> str:
    return os.path.join(_rebar_dir(tracker), _DRAIN_LOCK_NAME)


def _describe_drain_lock_holder(path: str) -> str:
    """Human-readable holder of the drain lock at *path*, for a log line.

    Decodes the v2 ownership stamp and adds the shared liveness verdict, so a wedge is
    attributable without running ``rebar doctor``. Descriptive only — nothing here decides
    whether the lock may be reclaimed (that is :func:`lock_owner.stamped_file_is_stale`)."""
    from rebar._store import lock_owner as _owner

    try:
        with open(path, encoding="utf-8") as fh:
            stamp = fh.read().strip()
    except OSError:
        return "an unreadable drain lock"
    age = _owner._mkdir_lock_age_s(path)
    held = f", held {age:.0f}s" if age is not None else ""
    fields = _owner._parse_v2_stamp(stamp) if stamp else None
    if not fields:
        # No holder to name — the age is then the ONLY signal, so it must still be said.
        return f"an unstamped drain lock (pre-stamp rebar, or a torn stamp){held}"
    return (
        f"host={fields['host']} pid={fields['pid']} ({_owner._describe_stamped_pid(fields)}){held}"
    )


def _acquire_advisory_lock(tracker: str) -> int | None:
    """Best-effort non-blocking advisory drain lock. Returns an open fd on success, or None
    if genuinely held by another drainer. NOT the store write lock and NOT the optimistic
    queue claim — this only stops two drain PROCESSES from redundant work.

    The lock file carries the SAME v2 ownership stamp the store's mkdir write lock writes,
    and a collision is adjudicated by the SAME shared decision table
    (:func:`lock_owner.stamped_file_is_stale`): pid-recycle qualification, refusal without
    proof, and the wall-clock ceiling are inherited, not re-invented. Before that, a drainer
    that died between acquire and release leaked this file forever and every later drain
    silently skipped (bug knavish-stimulated-bluebottle).

    A provably-orphaned lock is reclaimed LOUDLY and the create retried EXACTLY ONCE; a
    second collision means another drainer won the race, so we give up rather than loop.
    A lock whose holder the shared table will not condemn is always respected."""
    from rebar._store import lock_owner as _owner

    rebar_dir = _rebar_dir(tracker)
    try:
        os.makedirs(rebar_dir, exist_ok=True)
    except OSError:
        return None
    path = _drain_lock_path(tracker)
    for attempt in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if attempt or not _owner.stamped_file_is_stale(path):
                return None
            logger.warning(
                "enrich drain: reclaiming stale drain lock %s held by %s",
                path,
                _describe_drain_lock_holder(path),
            )
            try:
                os.unlink(path)
            except OSError:
                return None  # someone else got there first; let them drain
            continue
        except OSError:
            return None  # any other open failure: a drain concern never fails a write
        try:
            os.write(fd, (_owner._owner_stamp() + "\n").encode("utf-8"))
        except OSError:
            # Best-effort, exactly like the mkdir leg's stamp write: an unstamped lock is
            # still bounded by the shared wall-clock ceiling, so this cannot wedge.
            logger.warning("enrich drain: could not stamp %s; lock is unattributable", path)
        return fd
    return None


def _release_advisory_lock(tracker: str, fd: int) -> None:
    try:
        os.close(fd)
        os.unlink(_drain_lock_path(tracker))
    except OSError:
        pass


def _drain_log_path(tracker: str) -> str:
    return os.path.join(_rebar_dir(tracker), _DRAIN_LOG)


def status(tracker: str, *, now_ns: int | None = None, repo_root=None) -> dict[str, int]:
    """The queue status buckets (mutually exclusive) from the reducer:
    ``{pending, claimed, soaking}``."""
    from rebar.llm.overlap import queue as _queue

    now = now_ns if now_ns is not None else _queue._now_ns()
    pending = claimed = soaking = 0
    try:
        entries = os.listdir(tracker)
    except OSError:
        entries = []
    for name in entries:
        if name.startswith(".") or not os.path.isdir(os.path.join(tracker, name)):
            continue
        st = _queue.reduce_ticket(name, tracker, now_ns=now)
        if not st.get("enqueued") or st.get("done"):
            continue
        if st.get("pending"):
            pending += 1
        elif st.get("claimed"):
            claimed += 1
        elif (st.get("not_before_ns") or 0) > now:
            soaking += 1
    return {"pending": pending, "claimed": claimed, "soaking": soaking}


def _stale_digest_ids(tracker: str, repo_root) -> list[str]:
    """Self-healing fallback: tickets whose cached digest is PRESENT-STALE (content drifted
    since enrichment) — re-enriched even without a live queue entry, so a crash between cert
    and enqueue can never permanently miss a ticket."""
    from rebar.llm.overlap import digest_sidecar as ds

    out: list[str] = []
    try:
        entries = os.listdir(tracker)
    except OSError:
        return out
    for name in entries:
        if name.startswith(".") or not os.path.isdir(os.path.join(tracker, name)):
            continue
        if ds.freshness(name, tracker=tracker, repo_root=repo_root) == "present-stale":
            out.append(name)
    return out


def drain(tracker: str, *, once: bool = False, repo_root=None, runner=None) -> dict:
    """Claim + process soaked queue entries (+ self-healing stale-digest tickets), up to the
    batch cap. Best-effort per item: an enrich failure releases the claim (lease expiry) and
    the batch continues; the failed ticket is re-picked later. Returns a summary dict."""
    from rebar.llm.config import LLMConfig
    from rebar.llm.enrich import enrich
    from rebar.llm.overlap import digest_sidecar as ds
    from rebar.llm.overlap import queue as _queue

    cfg = LLMConfig.from_env(repo_root=repo_root)
    lock_fd = _acquire_advisory_lock(tracker)
    if lock_fd is None:
        # WARNING, not INFO: a skip is normal when two drainers overlap for a moment, but it
        # is also exactly what a wedged lock looks like, so name the holder rather than
        # leaving an operator to discover the wedge via doctor days later.
        logger.warning(
            "enrich drain: advisory lock held by %s; skipping (exit 0)",
            _describe_drain_lock_holder(_drain_lock_path(tracker)),
        )
        return {"skipped": "lock-held", "processed": 0}

    processed = 0
    try:
        now = _queue._now_ns()
        batch = 1 if once else cfg.overlap_drain_batch
        # Self-healing fallback: a ticket with a present-stale digest but no live queue entry
        # (e.g. a crash between cert and enqueue, or a post-enrich edit) is ENQUEUED here with
        # a zero soak so it becomes claimable — then the single claim path below handles it.
        for tid in _stale_digest_ids(tracker, repo_root):
            st = _queue.reduce_ticket(tid, tracker, now_ns=now)
            if not st["pending"] and not st["claimed"]:
                _queue.enqueue(tid, soak_min=0, repo_root=repo_root, now_ns=now - 1)
        # Primary: soaked+unclaimed queue entries (now including the self-heal enqueues).
        candidates = _queue.pending_enrichment(now, tracker)
        drainer = _drainer_id()
        for tid in candidates:
            if processed >= batch:
                break
            if not _queue.claim(
                tid,
                drainer,
                lease_ttl_min=cfg.overlap_lease_ttl_min,
                now_ns=now,
                repo_root=repo_root,
            ):
                continue  # lost the optimistic claim; another drainer has it
            try:
                result = enrich(ticket_id=tid, repo_root=repo_root, config=cfg, runner=runner)
                ds.emit(result["digest"], tid, model=cfg.model, repo_root=repo_root)
                _queue.mark_done(tid, repo_root=repo_root)
                processed += 1
            except Exception:
                logger.warning(
                    "enrich drain: enrich(%s) failed; will retry after lease", tid, exc_info=True
                )
        return {"processed": processed, "batch": batch}
    finally:
        _release_advisory_lock(tracker, lock_fd)


def _detach_kwargs() -> dict:
    """Platform detach kwargs. POSIX: a new session so the child outlives the parent. Windows
    (authored, API-derisked; NOT reached in v1 — maybe_drain no-ops on nt): DETACHED_PROCESS |
    CREATE_NO_WINDOW (constants exist only on Windows, referenced only inside this branch)."""
    if os.name == "nt":
        return {
            "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
        }
    return {"start_new_session": True, "close_fds": True}


def _spawn_detached_drain(tracker: str) -> None:
    """Detach a `rebar enrich --drain` child that outlives the current command (POSIX). Mirrors
    push.py's PYTHONPATH bootstrap so the bare-python child can import rebar."""
    src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    child_env = {**os.environ}
    child_env["PYTHONPATH"] = src + (
        os.pathsep + child_env["PYTHONPATH"] if child_env.get("PYTHONPATH") else ""
    )
    logdir = os.path.dirname(_drain_log_path(tracker))
    try:
        os.makedirs(logdir, exist_ok=True)
        log_fh = open(_drain_log_path(tracker), "a")  # noqa: SIM115 — handed to the detached child
    except OSError:
        log_fh = subprocess.DEVNULL  # type: ignore[assignment]
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, sys.argv[2]); "
                "from rebar.llm import enrich_drain; enrich_drain.drain(sys.argv[1])",
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
        logger.warning("enrich drain detach failed; continuing", exc_info=True)


def maybe_drain(tracker: str, *, repo_root=None) -> None:
    """The opportunistic gate on ordinary invocations. Cheap "is anything soaked?" check that
    no-ops in the common case; else, per overlap_drain: ``async`` detaches a drain child,
    ``always`` runs the drain inline (tests/CI), ``off`` no-ops. Windows is a v1 no-op (lock.py
    fcntl would crash a drain child). NEVER raises — a drain concern must not fail a write."""
    try:
        from rebar import config as _root_config
        from rebar.llm.config import LLMConfig, agents_extra_installed
        from rebar.llm.overlap import queue as _queue

        # Windows drain is a v1 no-op (lock.py's fcntl import would crash a drain child) —
        # check it FIRST, before any config Path resolution.
        if os.name == "nt":
            logger.info("enrich drain: windows drain deferred (v1 no-op)")
            return
        # Gate on the feature flag (the common default-off path) so an ordinary write pays
        # only one config read: no enrichment $ is spent unless overlap detection is on.
        if not _root_config.compose_config(repo_root).verify.suggest_duplicate_tickets:
            return
        cfg = LLMConfig.from_env(repo_root=repo_root)
        if cfg.overlap_drain == "off":
            return
        if not agents_extra_installed():
            return  # no LLM → clean no-op
        # Cheap gate: no-op fast when nothing is soaked+eligible. The gate-budget is
        # MEASURED and a breach is logged (observability) — a hard abort would drop
        # legitimate work, so the budget is an observed target, not a cutoff.
        gate_start = time.monotonic()
        soaked = bool(_queue.pending_enrichment(_queue._now_ns(), tracker))
        gate_ms = (time.monotonic() - gate_start) * 1000.0
        if gate_ms > cfg.overlap_drain_gate_budget_ms:
            logger.warning(
                "enrich drain gate check took %.1f ms (> %d ms budget)",
                gate_ms,
                cfg.overlap_drain_gate_budget_ms,
            )
        if not soaked:
            return
        if cfg.overlap_drain == "always":
            drain(tracker, repo_root=repo_root)  # synchronous inline (tests/CI)
        else:  # async
            _spawn_detached_drain(tracker)
    except Exception:
        logger.warning("maybe_drain failed; continuing", exc_info=True)


def cmd_enrich(argv: list[str], tracker: str) -> int:
    """`rebar enrich` CLI: `--drain` (bounded drain), `--once` (one entry), `status` (JSON)."""
    import json

    if argv and argv[0] == "status":
        sys.stdout.write(json.dumps(status(tracker)) + "\n")
        return 0
    once = "--once" in argv
    result = drain(tracker, once=once)
    sys.stdout.write(json.dumps(result) + "\n")
    return 0
