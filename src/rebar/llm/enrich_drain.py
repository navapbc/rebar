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


def _canonical_tracker(tracker: str) -> str:
    """*tracker* resolved through symlinks, degrading to the raw value.

    Delegates to :func:`rebar._store.lock.canonical_tracker` — the same resolution the store
    write lock uses — rather than re-deriving one, so a caller reaching the store through a
    symlink lands on the same paths as a caller holding its real path. Lazily imported and
    never-raising, matching this module's posture: the drain rides the tail of ordinary
    writes, where a background concern must not fail the operation that triggered it (the
    same reasoning as :func:`rebar._proc.detached_child_cwd`)."""
    try:
        from rebar._store import lock as _lock

        return _lock.canonical_tracker(tracker)
    except OSError:
        return tracker


def _rebar_dir(tracker: str) -> str:
    """The repo's ``.rebar/`` — the sibling of the CANONICAL ``.tickets-tracker`` dir.

    Resolving *tracker* first is load-bearing, not cosmetic (bug ``da68-fc7c-068c-4c53`` /
    ``nuclear-calm-heron``). A ``make worktree`` worktree's ``.tickets-tracker`` is a SYMLINK
    to the canonical store while its ``.rebar`` is a real per-worktree directory, so a bare
    ``dirname`` stops at the CALLER and keys the drain lock and the drain log on the view
    instead of the store. That defeated both: two agents in two worktrees held two DIFFERENT
    lock files while draining the SAME queue, and the log was written into — and deleted with
    — an ephemeral worktree. Doing the resolution HERE, inside the derivation, is what makes
    the invariant hold for every path helper at once and keeps a caller from defeating it
    (the shape ``_commands.compact_trigger._rebar_dir`` landed for the same class, bug
    ``93a9-66cf-e681-4f49``)."""
    return os.path.join(os.path.dirname(_canonical_tracker(tracker)), ".rebar")


def _drain_lock_path(tracker: str) -> str:
    return os.path.join(_rebar_dir(tracker), _DRAIN_LOCK_NAME)


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
    A lock whose holder the shared table will not condemn is always respected.

    Mutual exclusion here is DEFEASIBLE ACROSS THE RECLAIM, by design — see the comment on
    the ``os.unlink`` below for the window, the bounded harm, and why the fix is deferred."""
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
            # ACCEPTED RACE (task deathful-lettered-maltesedog, operator decision
            # 2026-08-16). Nothing excludes another drainer between the
            # `stamped_file_is_stale` verdict above and this unlink: both can condemn the
            # same orphan, the winner can recreate and stamp a FRESH lock, and this unlink
            # then removes that live lock — so two drains can run at once. (The mkdir leg
            # has the same shape but is safe, because `_acquire_mkdir`'s precondition is
            # that its caller already holds the exclusive fcntl leg; a single FILE lock has
            # no such kernel leg to inherit.) The window is left open deliberately:
            #   * Harm is bounded to REDUNDANT WORK, never to a wrong outcome. This lock is
            #     advisory and exists only to stop two drain PROCESSES duplicating effort;
            #     the correctness boundary is the per-ticket optimistic queue claim
            #     (`overlap.queue.claim`, lease-bounded), which a second drainer loses.
            #   * Closing it means changing the MECHANISM — a kernel leg (`flock`) or an
            #     atomic create-and-rename — which is a larger change than the bug it would
            #     prevent, on a path that is already strictly better than the permanent
            #     wedge it replaced (bug knavish-stimulated-bluebottle).
            #   * It is also self-limiting: the retry above is capped at ONE, so a contended
            #     reclaim converges rather than spinning.
            # Revisit only if drain work stops being idempotent, or if the queue claim
            # ceases to be the correctness boundary — either would turn "redundant" into
            # "wrong" and make the kernel leg worth its cost.
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
    batch cap. Best-effort per item: a TRANSIENT enrich failure releases the claim (lease expiry)
    and the batch continues, so the failed ticket is re-picked later; a per-item PERMANENT input
    rejection is tombstoned instead so it is attempted exactly once (see
    :func:`_record_item_failure`). Returns a summary dict."""
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
            except Exception as exc:  # noqa: BLE001 — logged+dispositioned by the helper below
                # Extracted whole (behaviour-preserving for every pre-existing failure) so the
                # new disposition branch lives OUTSIDE this already-long loop and adds no
                # cyclomatic complexity to `drain` — see the module-size/complexity policy.
                _record_item_failure(exc, tid, repo_root=repo_root)
        return {"processed": processed, "batch": batch}
    finally:
        _release_advisory_lock(tracker, lock_fd)


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
            _canonical_tracker(tracker),
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
