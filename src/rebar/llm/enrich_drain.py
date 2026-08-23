"""Tier-1 opportunistic detached enrichment drain + ``rebar enrich`` CLI backing
(epic only-crave-art, story c1de).

The overlap feature must run async across server/PC/Mac/cloud with ZERO client setup — no
daemon, broker, or scheduler. Following the git-gc-auto / npm-update-notifier pattern, a
cheap ``maybe_drain()`` gate on ordinary invocations no-ops in the common case, else DETACHES
the enrichment to a child that outlives the command (reusing push.py's POSIX detach). The
drainer runs three phases (the SKIP LOCKED job-queue shape — reserve-short,
process-unlocked, finalize-short, lease recovery): COLLECT claims soaked queue entries
under the advisory drain lock (optimistic per-ticket claims, per S4), ENRICH runs the LLM
calls (S1) with NO lock held, FINALIZE revalidates content and writes the digest (S2) +
DONE — bounded per run, self-healing on crash, at most ``_MAX_CONCURRENT_DRAINERS``
processes spending LLM $ at once.

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

from rebar._store.paths import StorePaths
from rebar._store.stamped_lock import release_stamped_lock, stamped_file_lock

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


def _drain_lock_path(tracker: str) -> str:
    return StorePaths(tracker).sidecar(_DRAIN_LOCK_NAME)


# Vocabulary for a drain lock whose stamp names no holder. These are VERBATIM the phrases
# ``rebar._commands.doctor_locks._existence_report`` renders (as "unknown (<phrase>)") for this
# same file, because the drain's WARNING and doctor's lock row describe ONE artifact and an
# operator reading both must not have to translate between two private dialects. They are
# duplicated rather than imported: the dependency would run llm -> _commands, i.e. a library
# module reaching into a CLI command. A test in tests/unit/test_enrich_drain.py
# (test_drain_and_doctor_describe_a_holderless_lock_identically) pins the agreement instead,
# so a change to either surface fails a test rather than confusing an operator.
_NO_STAMP = "no ownership stamp"
_UNRECOGNISED_STAMP = "unrecognised ownership stamp"
_INCOMPLETE_STAMP = "incomplete ownership stamp"


def _holderless_stamp_phrase(stamp: str, fields: dict[str, str] | None) -> str:
    """Which no-holder condition *stamp* is in, given ``_parse_v2_stamp``'s verdict.

    Three genuinely different situations, and an operator's next move differs by which:
    nothing was ever written (an orphan from a pre-stamp rebar, or the create/stamp window),
    a dialect this rebar does not recognise (a NEWER rebar's stamp — forward compatibility,
    so leave it alone), or a v2 stamp missing required fields (a torn mid-write read, i.e.
    very likely a LIVE drainer caught mid-acquire)."""
    if not stamp:
        return _NO_STAMP
    if fields is None:
        return _UNRECOGNISED_STAMP
    return _INCOMPLETE_STAMP


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
        return f"an unknown holder ({_holderless_stamp_phrase(stamp, fields)}){held}"
    return (
        f"host={fields['host']} pid={fields['pid']} ({_owner._describe_stamped_pid(fields)}){held}"
    )


def _acquire_advisory_lock(tracker: str) -> int | None:
    """Best-effort non-blocking advisory drain lock: an open fd, or ``None`` if another drainer
    holds it. NOT the store write lock and NOT the optimistic queue claim — it only stops two
    drain PROCESSES from redundant work. The mechanism is
    :func:`rebar._store.stamped_lock.stamped_file_lock`, shared with the compaction and
    snapshot-GC triggers; drain-specific and so still here are the sidecar directory (the shared
    helper creates none) and ``_describe_drain_lock_holder``, which names the holder in the
    reclaim WARNING so a wedge is attributable without ``rebar doctor``."""
    try:
        os.makedirs(StorePaths(tracker).rebar_dir, exist_ok=True)
    except OSError:
        return None
    return stamped_file_lock(
        _drain_lock_path(tracker),
        label="enrich drain",
        lock_noun="drain lock",
        describe_holder=_describe_drain_lock_holder,
    )


def _release_advisory_lock(tracker: str, fd: int) -> None:
    """Drop the drain lock. The shared release closes and unlinks in INDEPENDENT legs — this
    site did both in one ``try``, so a failing close leaked the lock for an hour (story 1cf6)."""
    release_stamped_lock(_drain_lock_path(tracker), fd)


def _drain_log_path(tracker: str) -> str:
    return StorePaths(tracker).log(_DRAIN_LOG)


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


def _is_permanent_input_rejection(exc: BaseException) -> bool:
    """Is *exc* a per-ITEM, deterministic input rejection (``ResolutionClass.CHANGE_INPUT``)?

    ``run_failure`` attaches the classifier's verdict to the raised provider error as
    ``exc.outcome`` (an :class:`rebar.llm.failure.LLMOutcome`). Read it DEFENSIVELY: an
    arbitrary exception may carry no ``outcome`` at all, or an ``outcome`` of any shape (a
    string, a mock, an object whose attribute access raises). Classification runs inside the
    drain's failure handler, and a drain concern must never fail a write — so anything
    unexpected here degrades to "not permanent", i.e. the pre-existing transient posture.

    WHY THIS CLASS AND NOT ``retryable=False``. ``retryable`` is false for six of the eight
    classes, and the others are per-ENVIRONMENT, not per-item: ``CHANGE_SETTINGS`` is what a
    401/403 bad credential maps to, ``CHANGE_PROVIDER_OR_MODEL`` is a provider outage,
    ``INCREASE_PROVIDER_LIMITS`` is a quota ceiling, ``NEEDS_INVESTIGATION`` is unknown. Every
    one of those fails identically for EVERY ticket in the batch, so tombstoning on
    ``retryable=False`` would silently drain the entire backlog to DONE during one outage or one
    expired key, and those digests would never be computed. ``CHANGE_INPUT`` is the only class
    that is a property of THIS ticket's bytes: a body the provider rejects as too large or
    malformed is rejected identically forever, so retrying it is pure waste.
    """
    from rebar.llm.failure import ResolutionClass

    try:
        outcome = getattr(exc, "outcome", None)
        # `ResolutionClass` is a `str` Enum, so this also matches the persisted plain-string
        # shape ("CHANGE_INPUT") a differently-constructed outcome may carry.
        return getattr(outcome, "resolution_class", None) == ResolutionClass.CHANGE_INPUT
    except Exception:  # noqa: BLE001 — a hostile __getattr__ must never fail the drain
        return False


def _record_item_failure(exc: BaseException, tid: str, *, repo_root) -> None:
    """Dispose of ONE failed per-item ``enrich()``: tombstone it, or leave it transient.

    Bug 569c-931f-69a2-4c1d (spongy-illjudged-terrier). The drain's blanket
    ``except Exception`` used to log "will retry after lease" for EVERY failure and never append
    ``DONE_ENRICH``. The queue reducer (``overlap/queue.py``) knows only ENQUEUE / CLAIM / DONE,
    so an entry with no DONE returns to ``pending`` the moment its lease expires and the next
    drain re-claims it — forever. For a permanently-rejected prompt that is an infinite loop:
    the live store showed 435 / 430 / 424 ``CLAIM_ENRICH`` events against 7 / 11 / 12
    ``DONE_ENRICH`` on the three affected tickets. A per-item permanent rejection must be
    attempted EXACTLY ONCE, so it gets the DONE tombstone and leaves the pending set for good.

    Note the tombstone's interaction with the self-heal: a ``CHANGE_INPUT`` tombstone leaves the
    ticket's digest ``absent`` (no ``ds.emit`` ran), and ``_stale_digest_ids`` re-enqueues only
    ``present-stale`` digests (``overlap.digest_sidecar.freshness``) — so the self-heal will NOT
    resurrect it. That is intended: the entry is genuinely terminal until the ticket is
    re-certified, which enqueues it naturally with fresh content. The companion fix in
    ``rebar.llm.enrich._bound_source`` means the re-enqueued attempt then fits the window, so
    this branch is the backstop for input the model rejects for some OTHER reason, not the
    routine path.

    Every other failure — a bare ``RuntimeError``, an error with no ``outcome``, and crucially
    any other non-retryable class — keeps the EXISTING transient posture: no DONE, re-pickable
    once the lease expires.
    """
    from rebar.llm.overlap import queue as _queue

    if not _is_permanent_input_rejection(exc):
        logger.warning(
            "enrich drain: enrich(%s) failed; will retry after lease", tid, exc_info=True
        )
        return

    from rebar.llm.failure import ResolutionClass, message_for

    # Deliberately worded so it cannot be mistaken for the transient line above: it names the
    # DISPOSITION ("permanently rejected", "will NOT retry") alongside the ticket, and reuses the
    # shared `RESOLUTION_MESSAGES` remediation text via `message_for` so an operator sees the same
    # advice the CLIs print for this class.
    logger.warning(
        "enrich drain: enrich(%s) permanently rejected (%s) — marking the queue entry done; "
        "will NOT retry. %s",
        tid,
        ResolutionClass.CHANGE_INPUT.value,
        message_for(ResolutionClass.CHANGE_INPUT.value) or "",
        exc_info=True,
    )
    try:
        _queue.mark_done(tid, repo_root=repo_root)
    except Exception:
        # The tombstone is best-effort like every other drain write: failing to append it just
        # restores the old (looping) behaviour for this entry, and must never fail the drain.
        logger.warning("enrich drain: could not tombstone %s; it stays pending", tid, exc_info=True)


def drain(tracker: str, *, once: bool = False, repo_root=None, runner=None) -> dict:
    """Claim + process soaked queue entries (+ self-healing stale-digest tickets), up to the
    lease-bounded batch cap, in three phases so the advisory drain lock is NEVER held across
    an LLM call (bug 6148-5d81-8e80-41e8 — 437s/131s observed holds made the drain
    single-flight; the SKIP LOCKED job-queue shape: reserve-short, process-unlocked,
    finalize-short, lease recovery):

    - COLLECT (:func:`_collect_claims`, under the drain lock, seconds): concurrency guard,
      self-heal re-enqueues, pending scan, optimistic per-ticket claims + content snapshots.
    - ENRICH (:func:`_enrich_claims`, NO lock): the LLM calls. A hung provider now blocks
      only this process's batch — other drainers keep collecting and processing.
    - FINALIZE (:func:`_finalize_claims`, no drain lock; the store write lock per append,
      unchanged): revalidate each snapshot, then digest emit + DONE, stale re-enqueue, or
      failure disposition.

    Best-effort per item exactly as before: a TRANSIENT enrich failure releases the claim
    (lease expiry) and the batch continues; a per-item PERMANENT input rejection is
    tombstoned so it is attempted exactly once (see :func:`_record_item_failure`). Returns a
    summary dict."""
    from rebar.llm.config import LLMConfig
    from rebar.llm.overlap import queue as _queue

    cfg = LLMConfig.from_env(repo_root=repo_root)
    now = _queue._now_ns()
    batch = 1 if once else _lease_bounded_batch(cfg)
    skip, items = _collect_claims(tracker, batch=batch, cfg=cfg, repo_root=repo_root, now=now)
    if skip is not None:
        return skip
    results = _enrich_claims(items, cfg=cfg, repo_root=repo_root, runner=runner)
    processed, stale_skipped = _finalize_claims(results, cfg=cfg, repo_root=repo_root)
    summary = {"processed": processed, "batch": batch}
    if stale_skipped:
        summary["stale_skipped"] = stale_skipped
    return summary


# The maximum number of drain processes allowed to spend LLM $ concurrently (operator ruling
# OQ1 on bug 6148-5d81-8e80-41e8: 2-3, bounded by a small guard). With the lock no longer held
# across LLM calls, every per-write spawned child is a would-be drainer; unbounded, a busy
# store could fan out one LLM batch per write. A module constant, not a config door — the
# ruling asked for a small fixed bound, and the restructure deliberately adds no config keys.
_MAX_CONCURRENT_DRAINERS = 3

# Worst-case per-item budget (seconds) used to derive the lease-bounded batch cap (operator
# ruling OQ3): the trivial-class digest call is single-turn, tool-less structured extraction,
# comfortably under 40s per item, so lease_ttl // 40 items cannot outrun their lease mid-run
# (15 min default lease -> 22, inside the ruled ~20-25 window).
_WORST_CASE_ITEM_S = 40


def _lease_bounded_batch(cfg) -> int:
    """The effective per-run claim-window size: the configured ``overlap_drain_batch``
    clamped to what the claim lease can cover (``lease_ttl_s // _WORST_CASE_ITEM_S``), never
    below 1. A batch that outruns its lease causes duplicate claims mid-run (OQ3)."""
    lease_bound = max(1, (cfg.overlap_lease_ttl_min * 60) // _WORST_CASE_ITEM_S)
    return max(1, min(cfg.overlap_drain_batch, lease_bound))


def _live_drainer_ids(tracker: str, now: int) -> set[str]:
    """Distinct ``drainer_id``s holding a live-lease claim right now — the concurrency
    guard's census. Reads each claimed ticket's latest post-enqueue CLAIM event; a crashed
    drainer's ids age out with its leases, so the guard is self-healing."""
    from rebar.llm.overlap import queue as _queue

    out: set[str] = set()
    try:
        entries = os.listdir(tracker)
    except OSError:
        return out
    for name in entries:
        if name.startswith(".") or not os.path.isdir(os.path.join(tracker, name)):
            continue
        if not _queue.reduce_ticket(name, tracker, now_ns=now).get("claimed"):
            continue
        latest = _queue._latest(os.path.join(tracker, name), _queue.CLAIM)
        did = (latest[2] or {}).get("drainer_id") if latest else None
        if did:
            out.add(str(did))
    return out


def _snapshot_hash(tid: str, repo_root) -> str | None:
    """The claimed ticket's ``digest_sidecar.content_hash`` at collect time — the finalize
    phase's revalidation key. ``None`` (an unreadable state) disables revalidation for this
    item, degrading to the pre-restructure emit-without-revalidation behaviour."""
    from rebar import _reads
    from rebar.llm.overlap import digest_sidecar as ds

    try:
        return ds.content_hash(_reads.show_ticket(tid, repo_root=repo_root))
    except Exception:  # noqa: BLE001 — a drain concern must never fail the drain
        return None


def _collect_claims(
    tracker: str, *, batch: int, cfg, repo_root, now: int
) -> tuple[dict | None, list[tuple[str, str | None]]]:
    """PHASE COLLECT, the only phase under the drain advisory lock: concurrency guard,
    self-heal re-enqueues, the pending scan, and up to *batch* optimistic claims with a
    content-hash snapshot each. Returns ``(skip_summary, [])`` when the drain must not run
    (lock held / drainer cap), else ``(None, claimed_items)`` — and the lock is RELEASED
    before the caller's enrich phase either way."""
    from rebar.llm.overlap import queue as _queue

    lock_fd = _acquire_advisory_lock(tracker)
    if lock_fd is None:
        # WARNING, not INFO: a skip is normal when two drainers overlap for a moment, but it
        # is also exactly what a wedged lock looks like, so name the holder rather than
        # leaving an operator to discover the wedge via doctor days later.
        logger.warning(
            "enrich drain: advisory lock held by %s; skipping (exit 0)",
            _describe_drain_lock_holder(_drain_lock_path(tracker)),
        )
        return {"skipped": "lock-held", "processed": 0}, []
    try:
        others = _live_drainer_ids(tracker, now)
        if len(others) >= _MAX_CONCURRENT_DRAINERS:
            logger.info(
                "enrich drain: %d drainers already hold live claims (cap %d); skipping",
                len(others),
                _MAX_CONCURRENT_DRAINERS,
            )
            return {"skipped": "concurrency-cap", "processed": 0}, []
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
        items: list[tuple[str, str | None]] = []
        for tid in candidates:
            if len(items) >= batch:
                break
            if not _queue.claim(
                tid,
                drainer,
                lease_ttl_min=cfg.overlap_lease_ttl_min,
                now_ns=now,
                repo_root=repo_root,
            ):
                continue  # lost the optimistic claim; another drainer has it
            items.append((tid, _snapshot_hash(tid, repo_root)))
        return None, items
    finally:
        _release_advisory_lock(tracker, lock_fd)


def _enrich_claims(
    items: list[tuple[str, str | None]], *, cfg, repo_root, runner
) -> list[tuple[str, str | None, dict | None, BaseException | None]]:
    """PHASE ENRICH — the LLM calls, with NO lock held. Per item: the collect snapshot plus
    either the enrich result or the exception it raised (dispositioned in finalize)."""
    from rebar.llm.enrich import enrich

    results: list[tuple[str, str | None, dict | None, BaseException | None]] = []
    for tid, snap in items:
        try:
            result = enrich(ticket_id=tid, repo_root=repo_root, config=cfg, runner=runner)
            results.append((tid, snap, result, None))
        except Exception as exc:  # noqa: BLE001 — logged+dispositioned in _finalize_claims
            results.append((tid, snap, None, exc))
    return results


def _finalize_claims(
    results: list[tuple[str, str | None, dict | None, BaseException | None]], *, cfg, repo_root
) -> tuple[int, int]:
    """PHASE FINALIZE — short writes only (each under the store write lock per append, as
    every queue write always was). Per item: a failure keeps its existing disposition
    (:func:`_record_item_failure`); a ticket whose content hash drifted since collect gets NO
    stale digest — the emit is skipped and the entry re-enqueued with soak 0 (immediately
    claimable, matching the self-heal path's posture; staleness is handled BEFORE the write
    instead of by the self-heal loop after it); else digest emit + DONE. Returns
    ``(processed, stale_skipped)``."""
    from rebar import _reads
    from rebar.llm.overlap import digest_sidecar as ds
    from rebar.llm.overlap import queue as _queue

    processed = stale_skipped = 0
    for tid, snap, result, exc in results:
        if exc is not None or result is None:
            _record_item_failure(
                exc or RuntimeError("enrich returned nothing"), tid, repo_root=repo_root
            )
            continue
        state = None
        if snap is not None:
            try:
                state = _reads.show_ticket(tid, repo_root=repo_root)
            except Exception:  # noqa: BLE001 — degrade to emit-without-revalidation
                state = None
            if state is not None and ds.content_hash(state) != snap:
                stale_skipped += 1
                logger.info(
                    "enrich drain: %s changed during enrichment; digest skipped, re-enqueued",
                    tid,
                )
                _queue.enqueue(tid, soak_min=0, repo_root=repo_root)
                continue
        ds.emit(result["digest"], tid, state=state, model=cfg.model, repo_root=repo_root)
        _queue.mark_done(tid, repo_root=repo_root)
        processed += 1
    return processed, stale_skipped


def _spawn_detached_drain(tracker: str) -> None:
    """Detach a `rebar enrich --drain` child that outlives the current command (POSIX), via
    the shared detached-rebar-child spawner (:func:`rebar._proc.spawn_detached`), which owns
    the PYTHONPATH bootstrap, the ``-c`` re-entry stub, the detach flags and the durable-cwd
    anchor. This site keeps its own stderr sink (the store-scoped drain log), its canonical
    tracker argument, and its broad never-raise catch."""
    from rebar._proc import spawn_detached

    logdir = os.path.dirname(_drain_log_path(tracker))
    try:
        os.makedirs(logdir, exist_ok=True)
        log_fh = open(_drain_log_path(tracker), "a")  # noqa: SIM115 — handed to the detached child
    except OSError:
        log_fh = subprocess.DEVNULL  # type: ignore[assignment]
    try:
        spawn_detached(
            "rebar.llm.enrich_drain",
            "drain",
            # The canonical store tracker, for the same reason spawn_detached resolves the
            # child's cwd: the child outlives the worktree that spawned it, and every path it
            # touches — the store it lists, the queue events it appends, the digests it emits
            # — is derived from this argument. Handing over a worktree's SYMLINK leaves the
            # running child pointed at a path that dies with the worktree
            # (bug da68-fc7c-068c-4c53: "Error: cannot list '<retired>/.tickets-tracker'").
            StorePaths(tracker).canonical,
            env={**os.environ},
            stderr=log_fh,
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
        # Cheap gate: no-op fast when nothing is soaked+eligible. EXISTENCE-only — the gate
        # needs a yes/no, and the list probe's O(backlog) enumeration priced every store
        # write at seconds once a standing backlog existed (bug 6148-5d81-8e80-41e8). The
        # gate-budget is MEASURED and a breach is logged (observability) — a hard abort
        # would drop legitimate work, so the budget is an observed target, not a cutoff.
        gate_start = time.monotonic()
        soaked = _queue.has_pending_enrichment(_queue._now_ns(), tracker)
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

    from rebar._cli._parser import ParseError
    from rebar._cli._parsers.advanced.enrich import build

    try:
        ns, _unknown = build(prog="rebar enrich").parse_known_args(argv)
        mode, once = ns.mode, ns.once
    except ParseError:
        # Preserve the historically lenient contract: a non-"status" positional is
        # treated as a drain request (never a hard reject), mirroring the pre-factory
        # `argv[0] == "status"` / `"--once" in argv` walk.
        mode, once = None, "--once" in argv

    if mode == "status":
        sys.stdout.write(json.dumps(status(tracker)) + "\n")
        return 0
    result = drain(tracker, once=once)
    sys.stdout.write(json.dumps(result) + "\n")
    return 0
