"""Tier-1 opportunistic detached enrichment drain + ``rebar enrich`` CLI backing
(epic only-crave-art, story c1de).

The overlap feature must run async across server/PC/Mac/cloud with ZERO client setup — no
daemon, broker, or scheduler. Following the git-gc-auto / npm-update-notifier pattern, a
cheap ``maybe_drain()`` gate on ordinary invocations no-ops in the common case, else DETACHES
the enrichment to a child that outlives the command (reusing push.py's POSIX detach). The
drainer loop claims soaked queue entries (optimistic, per S4), runs enrich (S1), writes the
digest (S2), marks done, and prunes — bounded per run, self-healing on crash.

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


def _acquire_advisory_lock(tracker: str) -> int | None:
    """Best-effort non-blocking advisory drain lock. Returns an open fd on success, or None
    if already held (the caller then skips silently). NOT the store write lock and NOT the
    optimistic queue claim — this only stops two drain PROCESSES from redundant work."""
    rebar_dir = _rebar_dir(tracker)
    try:
        os.makedirs(rebar_dir, exist_ok=True)
        path = os.path.join(rebar_dir, _DRAIN_LOCK_NAME)
        return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return None
    except OSError:
        return None


def _release_advisory_lock(tracker: str, fd: int) -> None:
    try:
        os.close(fd)
        os.unlink(os.path.join(_rebar_dir(tracker), _DRAIN_LOCK_NAME))
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
        logger.info("enrich drain: advisory lock held by another drain; skipping (exit 0)")
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
                _prune_queue_events(tid, tracker)
                processed += 1
            except Exception:  # noqa: BLE001 — per-item best-effort; failed item re-picked after lease
                logger.warning(
                    "enrich drain: enrich(%s) failed; will retry after lease", tid, exc_info=True
                )
        return {"processed": processed, "batch": batch}
    finally:
        _release_advisory_lock(tracker, lock_fd)


def _prune_queue_events(ticket_id: str, tracker: str) -> None:
    """Bound queue growth: keep only the latest queue event per ticket (the DONE tombstone),
    dropping superseded ENQUEUE/CLAIM/older-DONE. Best-effort, git-backed."""
    from rebar.llm.overlap import queue as _queue

    rid_dir = os.path.join(tracker, _queue._resolve(ticket_id, tracker))
    try:
        files = sorted(
            f
            for f in os.listdir(rid_dir)
            if any(f.endswith(f"-{et}.json") for et in _queue.QUEUE_EVENT_TYPES)
            and not f.startswith(".")
        )
    except OSError:
        return
    old = files[:-1]  # keep the single newest queue event
    if not old:
        return
    try:
        from rebar._store.event_append import delete_events

        rid = _queue._resolve(ticket_id, tracker)
        rels = [f"{rid}/{f}" for f in old]
        # Delete through the canonical locked write path (bug malevolent-emigratory-umbrette):
        # a raw git rm + whole-index commit here races normal store writes — it sweeps a
        # concurrent locked writer's just-staged event into this prune commit (sweep-and-strand)
        # and advances HEAD under the writer's ref update. delete_events serializes under the
        # unified write lock and commits ONLY these paths (pathspec-scoped).
        delete_events(tracker, rels, f"prune: enrich queue {rid}")
    except Exception:  # noqa: BLE001 — best-effort prune; never fails the drain
        logger.warning("enrich queue prune failed; continuing", exc_info=True)


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
    except Exception:  # noqa: BLE001 — detach is best-effort; a spawn failure never fails the parent
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
        if not _root_config.load_config(repo_root).verify.overlap_enabled:
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
    except Exception:  # noqa: BLE001 — a drain concern must NEVER fail the triggering write
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
