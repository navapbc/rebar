"""Fail-closed publication of an isolated completion-close candidate."""

from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from rebar import config
from rebar._commands._seam import CommandError
from rebar._commands.completion_candidate import CompletionCandidate
from rebar._snapshot.ticket_view import (
    CompletionReadBasis,
    ReceiptValidation,
    TicketsOID,
    tracker_head,
    validate_receipt,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryResult:
    state: str
    metrics: dict[str, int]
    commit_oid: TicketsOID
    retry: bool = False
    idempotent: bool = False


EquivalentClose = Callable[[str, str, CompletionReadBasis, TicketsOID], bool]


def _elapsed_ms(started_ns: int) -> int:
    return (time.monotonic_ns() - started_ns) // 1_000_000


def _metrics(
    started_ns: int,
    *,
    merges: int = 0,
    push_attempts: int = 1,
    receipt_validation_ms: int = 0,
) -> dict[str, int]:
    return {
        "atomic_close_push_ms": _elapsed_ms(started_ns),
        "atomic_close_push_attempts": push_attempts,
        "atomic_close_merges": merges,
        "atomic_close_delivery_receipt_validation_ms": receipt_validation_ms,
    }


def _validate(
    tracker: str,
    basis: CompletionReadBasis,
    current: TicketsOID,
) -> tuple[ReceiptValidation, int]:
    started = time.monotonic_ns()
    validation = validate_receipt(tracker, basis.receipt, current_oid=current)
    return validation, _elapsed_ms(started)


def _concurrency(message: str) -> CommandError:
    from rebar._commands.txn import ConcurrencyMismatch

    return ConcurrencyMismatch(message)


def _receipt_conflict(validation: ReceiptValidation, ticket_id: str) -> CommandError:
    detail = ", ".join(validation.conflicts[:12]) or "unknown ticket-store drift"
    return _concurrency(
        f"Error: cannot close {ticket_id}: ticket material read by completion changed "
        f"before publication ({detail}); run completion verification again"
    )


def _record_local_sync_pending(tracker: str, reason: str, detail: str, remote_ref: str) -> None:
    """Record only post-publication local convergence or delivery work.

    Before the remote accepts a candidate there is no shared local close to deliver, so an
    ordinary failure is returned directly and must not poison generic push state.  Once the
    remote accepted it, a failed local fetch/merge or a concurrent local commit is genuine
    pending synchronization/delivery work.
    """
    from rebar._store import push_state

    push_state.record_failure(tracker, reason, detail, remote_ref)
    sys.stderr.write(
        "Warning: certified close reached the ticket remote but ticket-store delivery "
        f"still has local work pending ({reason}: {detail}).\n"
    )


def _merge_ref(tracker: str, ref: str, *, expected_head: TicketsOID | None = None) -> bool | None:
    """Merge a fetched ref under the store lock; ``None`` means local HEAD raced."""
    from rebar._store import lock as store_lock
    from rebar._store import push

    try:
        with store_lock.write_lock(tracker, timeout=10, attempts=1, dual_window=True):
            if expected_head is not None and tracker_head(tracker) != expected_head:
                return None
            merged = push._merge_remote_under_lock(push, tracker, ref, 1, True, store_lock)
    except Exception as exc:  # noqa: BLE001 — normalize the store boundary
        raise CommandError(
            f"Error: completion candidate merge failed: {exc}", returncode=1
        ) from None
    if not merged:
        raise CommandError("Error: completion candidate merge did not complete", returncode=1)
    return True


def _delete_private_ref(tracker: str, ref: str, oid: TicketsOID) -> None:
    from rebar._store import push

    # Expected-old guards against deleting a ref reused by anything else.  UUID naming makes
    # that theoretical, but the guard keeps cleanup ownership explicit.
    deleted = push._git(  # raw-git-ok: delete this operation's UUID-scoped private ref
        tracker, "update-ref", "-d", ref, oid.value
    )
    if deleted.returncode != 0:
        logger.warning(
            "could not delete private completion ref %s: %s",
            ref,
            (deleted.stderr or deleted.stdout or "unknown update-ref failure").strip(),
        )


def _import_candidate(candidate: CompletionCandidate, tracker: str) -> str:
    """Import the private commit under a run-unique ref, never shared ``HEAD``."""
    from rebar._store import push

    assert candidate.commit_oid is not None
    private_ref = f"refs/rebar/completion-candidates/{uuid.uuid4()}"
    imported = push._git(
        tracker,
        "fetch",
        candidate.tracker,
        f"HEAD:{private_ref}",
    )
    if imported.returncode != 0:
        detail = (imported.stderr or imported.stdout or "candidate import failed").strip()
        raise CommandError(f"Error: completion candidate import failed: {detail}", returncode=1)
    return private_ref


def _deliver_local(
    candidate: CompletionCandidate,
    tracker: str,
    ticket_id: str,
    basis: CompletionReadBasis,
    equivalent_close: EquivalentClose,
    *,
    started_ns: int,
) -> DeliveryResult:
    """Import and merge a candidate when the tracker has no configured Git remote."""
    assert candidate.commit_oid is not None
    current = tracker_head(tracker)
    validation, validation_ms = _validate(tracker, basis, current)
    if not validation.valid:
        if equivalent_close(tracker, ticket_id, basis, current):
            return DeliveryResult(
                "already_present",
                _metrics(
                    started_ns,
                    push_attempts=0,
                    receipt_validation_ms=validation_ms,
                ),
                current,
                idempotent=True,
            )
        raise _receipt_conflict(validation, ticket_id)
    private_ref = _import_candidate(candidate, tracker)
    try:
        merged = _merge_ref(tracker, private_ref, expected_head=current)
        if merged is None:
            return DeliveryResult(
                "retry_local_race",
                _metrics(
                    started_ns,
                    push_attempts=0,
                    receipt_validation_ms=validation_ms,
                ),
                candidate.commit_oid,
                retry=True,
            )
        return DeliveryResult(
            "local_only",
            _metrics(
                started_ns,
                merges=1,
                push_attempts=0,
                receipt_validation_ms=validation_ms,
            ),
            candidate.commit_oid,
        )
    finally:
        _delete_private_ref(tracker, private_ref, candidate.commit_oid)


def _fetch_remote(tracker: str, remote: str, branch: str) -> tuple[str, TicketsOID]:
    from rebar._store import push

    remote_ref = f"{remote}/{branch}"
    fetched_ref = f"refs/rebar/completion-fetches/{uuid.uuid4()}"
    fetched = push._git(
        tracker,
        "fetch",
        remote,
        f"refs/heads/{branch}:{fetched_ref}",
    )
    if fetched.returncode != 0:
        detail = (fetched.stderr or fetched.stdout or "ordinary fetch failed").strip()
        raise CommandError(f"Error: completion delivery fetch failed: {detail}", returncode=1)
    resolved = push._git(tracker, "rev-parse", fetched_ref)
    if resolved.returncode != 0:
        push._git(  # raw-git-ok: discard this operation's UUID-scoped fetch ref
            tracker, "update-ref", "-d", fetched_ref
        )
        raise CommandError(
            f"Error: completion delivery remote ref is missing: {remote_ref}", returncode=1
        )
    return fetched_ref, TicketsOID(resolved.stdout.strip())


def _candidate_reached_remote(
    tracker: str, candidate_oid: TicketsOID, remote_oid: TicketsOID
) -> bool:
    """Whether a failed push nevertheless made the candidate reachable remotely."""
    from rebar._store import push

    reached = push._git(
        tracker,
        "merge-base",
        "--is-ancestor",
        candidate_oid.value,
        remote_oid.value,
    )
    return reached.returncode == 0


def _converge_fetched_publication(
    tracker: str,
    fetched_ref: str,
    remote_oid: TicketsOID,
    result_oid: TicketsOID,
    *,
    remote_ref: str,
    started_ns: int,
    push_attempts: int,
    state: str,
    idempotent: bool = False,
    receipt_validation_ms: int = 0,
) -> DeliveryResult:
    """Merge an accepted remote publication without hiding concurrent local backlog."""
    from rebar._store import push_state

    try:
        _merge_ref(tracker, fetched_ref)
    except CommandError as exc:
        _record_local_sync_pending(tracker, f"{state}-local-sync", exc.message, remote_ref)
        return DeliveryResult(
            f"{state}_local_pending",
            _metrics(
                started_ns,
                push_attempts=push_attempts,
                receipt_validation_ms=receipt_validation_ms,
            ),
            result_oid,
            idempotent=idempotent,
        )
    try:
        local_oid = tracker_head(tracker)
    except Exception as exc:  # noqa: BLE001 — publication already happened; preserve that fact
        _record_local_sync_pending(
            tracker,
            f"{state}-local-head",
            f"could not confirm the converged local tracker revision: {exc}",
            remote_ref,
        )
        return DeliveryResult(
            f"{state}_local_pending",
            _metrics(
                started_ns,
                merges=1,
                push_attempts=push_attempts,
                receipt_validation_ms=receipt_validation_ms,
            ),
            result_oid,
            idempotent=idempotent,
        )
    if local_oid == remote_oid:
        # The accepted remote revision is the whole local committed store.  Only in this
        # exact case may this operation clear a generic marker; a different local HEAD can
        # belong to a concurrent writer whose delivery outcome we do not own.
        push_state.clear(tracker)
        return DeliveryResult(
            state,
            _metrics(
                started_ns,
                merges=1,
                push_attempts=push_attempts,
                receipt_validation_ms=receipt_validation_ms,
            ),
            result_oid,
            idempotent=idempotent,
        )
    _record_local_sync_pending(
        tracker,
        f"{state}-local-ahead",
        "the shared tracker also contains concurrent local commits; its next successful "
        "tickets-branch push will publish the merged backlog",
        remote_ref,
    )
    return DeliveryResult(
        f"{state}_local_pending",
        _metrics(
            started_ns,
            merges=1,
            push_attempts=push_attempts,
            receipt_validation_ms=receipt_validation_ms,
        ),
        result_oid,
        idempotent=idempotent,
    )


def _post_push_converge(
    tracker: str,
    remote: str,
    branch: str,
    candidate_oid: TicketsOID,
    *,
    started_ns: int,
    push_attempts: int,
    state: str = "pushed",
) -> DeliveryResult:
    """Bring the shared checkout to an already-published remote candidate."""
    remote_ref = f"{remote}/{branch}"
    fetched_ref: str | None = None
    remote_oid: TicketsOID | None = None
    try:
        fetched_ref, remote_oid = _fetch_remote(tracker, remote, branch)
    except CommandError as exc:
        _record_local_sync_pending(tracker, "published-local-sync", exc.message, remote_ref)
        return DeliveryResult(
            "pushed_local_pending",
            _metrics(started_ns, push_attempts=push_attempts),
            candidate_oid,
        )
    try:
        if not _candidate_reached_remote(tracker, candidate_oid, remote_oid):
            raise _concurrency(
                "Error: the remote tickets branch no longer contains the completion "
                "candidate after push acknowledgement; refusing to report publication"
            )
        return _converge_fetched_publication(
            tracker,
            fetched_ref,
            remote_oid,
            candidate_oid,
            remote_ref=remote_ref,
            started_ns=started_ns,
            push_attempts=push_attempts,
            state=state,
        )
    finally:
        if fetched_ref is not None and remote_oid is not None:
            _delete_private_ref(tracker, fetched_ref, remote_oid)


def _recover_remote_advance(
    candidate: CompletionCandidate,
    tracker: str,
    ticket_id: str,
    basis: CompletionReadBasis,
    equivalent_close: EquivalentClose,
    fetched_ref: str,
    remote_oid: TicketsOID,
    *,
    remote_ref: str,
    started_ns: int,
    push_attempts: int,
) -> DeliveryResult:
    """Classify a fetched remote advance as equivalent, unrelated, or conflicting."""
    assert candidate.commit_oid is not None
    validation, validation_ms = _validate(tracker, basis, remote_oid)
    if not validation.valid:
        if equivalent_close(tracker, ticket_id, basis, remote_oid):
            return _converge_fetched_publication(
                tracker,
                fetched_ref,
                remote_oid,
                remote_oid,
                remote_ref=remote_ref,
                started_ns=started_ns,
                push_attempts=push_attempts,
                state="already_present",
                idempotent=True,
                receipt_validation_ms=validation_ms,
            )
        raise _receipt_conflict(validation, ticket_id)
    _merge_ref(tracker, fetched_ref)
    return DeliveryResult(
        "retry_remote_advance",
        _metrics(
            started_ns,
            merges=1,
            push_attempts=push_attempts,
            receipt_validation_ms=validation_ms,
        ),
        candidate.commit_oid,
        retry=True,
    )


def _fetch_and_recover_remote_advance(
    candidate: CompletionCandidate,
    tracker: str,
    ticket_id: str,
    basis: CompletionReadBasis,
    remote: str,
    branch: str,
    equivalent_close: EquivalentClose,
    *,
    started_ns: int,
    push_attempts: int,
) -> DeliveryResult:
    fetched_ref, remote_oid = _fetch_remote(tracker, remote, branch)
    try:
        return _recover_remote_advance(
            candidate,
            tracker,
            ticket_id,
            basis,
            equivalent_close,
            fetched_ref,
            remote_oid,
            remote_ref=f"{remote}/{branch}",
            started_ns=started_ns,
            push_attempts=push_attempts,
        )
    finally:
        _delete_private_ref(tracker, fetched_ref, remote_oid)


def _probe_ambiguous_push(
    candidate: CompletionCandidate,
    tracker: str,
    ticket_id: str,
    basis: CompletionReadBasis,
    remote: str,
    branch: str,
    equivalent_close: EquivalentClose,
    *,
    started_ns: int,
    push_attempts: int,
) -> DeliveryResult | None:
    """Resolve a transport failure that may have happened after remote acceptance."""
    assert candidate.commit_oid is not None
    try:
        fetched_ref, remote_oid = _fetch_remote(tracker, remote, branch)
    except CommandError:
        return None
    try:
        if _candidate_reached_remote(tracker, candidate.commit_oid, remote_oid):
            return _converge_fetched_publication(
                tracker,
                fetched_ref,
                remote_oid,
                candidate.commit_oid,
                remote_ref=f"{remote}/{branch}",
                started_ns=started_ns,
                push_attempts=push_attempts,
                state="pushed_after_ambiguous_ack",
            )
        if remote_oid == candidate.base_oid:
            return None
        return _recover_remote_advance(
            candidate,
            tracker,
            ticket_id,
            basis,
            equivalent_close,
            fetched_ref,
            remote_oid,
            remote_ref=f"{remote}/{branch}",
            started_ns=started_ns,
            push_attempts=push_attempts,
        )
    finally:
        _delete_private_ref(tracker, fetched_ref, remote_oid)


def _push_remote_candidate(
    candidate: CompletionCandidate,
    tracker: str,
    ticket_id: str,
    basis: CompletionReadBasis,
    remote: str,
    branch: str,
    equivalent_close: EquivalentClose,
    *,
    started_ns: int,
) -> DeliveryResult:
    """Publish with bounded ambiguity recovery and ordinary fast-forward semantics."""
    from rebar._store import git_outcome, push, push_classify

    assert candidate.commit_oid is not None
    push_env = {**os.environ, "PRE_COMMIT_ALLOW_NO_CONFIG": "1"}
    private_ref = _import_candidate(candidate, tracker)
    remote_ref = f"{remote}/{branch}"
    healed_multi_bundle = False
    try:
        for push_attempts in range(1, push_classify._MAX_TRANSPORT_ATTEMPTS + 1):
            sent = push._git(
                tracker,
                "push",
                remote,
                f"{private_ref}:refs/heads/{branch}",
                env=push_env,
            )
            if sent.returncode == 0:
                return _post_push_converge(
                    tracker,
                    remote,
                    branch,
                    candidate.commit_oid,
                    started_ns=started_ns,
                    push_attempts=push_attempts,
                )
            stderr = sent.stderr or ""
            if push_classify._is_multi_bundle(stderr) and not healed_multi_bundle:
                try:
                    healed_multi_bundle = push_classify._heal_multi_bundle_or_stop(
                        tracker,
                        remote,
                        branch,
                        remote_ref,
                        stderr,
                        True,
                    )
                except push_classify.PushDeliveryError as exc:
                    raise CommandError(
                        f"Error: completion delivery could not heal the S3 remote: {exc.message}",
                        returncode=1,
                    ) from None
                if healed_multi_bundle:
                    continue
            if push._is_non_fast_forward(stderr):
                return _fetch_and_recover_remote_advance(
                    candidate,
                    tracker,
                    ticket_id,
                    basis,
                    remote,
                    branch,
                    equivalent_close,
                    started_ns=started_ns,
                    push_attempts=push_attempts,
                )
            outcome = git_outcome.classify(sent, operation=git_outcome.PUSH)
            if outcome.kind is git_outcome.GitKind.TRANSPORT:
                observed = _probe_ambiguous_push(
                    candidate,
                    tracker,
                    ticket_id,
                    basis,
                    remote,
                    branch,
                    equivalent_close,
                    started_ns=started_ns,
                    push_attempts=push_attempts,
                )
                if observed is not None:
                    return observed
            retriable = outcome.kind in (
                git_outcome.GitKind.TRANSPORT,
                git_outcome.GitKind.TRANSIENT_FS,
            )
            if retriable and push_attempts < push_classify._MAX_TRANSPORT_ATTEMPTS:
                push_classify._transport_backoff(push_attempts)
                continue
            detail = (stderr or sent.stdout or "ordinary push failed").strip()
            raise CommandError(f"Error: completion delivery push failed: {detail}", returncode=1)
    finally:
        _delete_private_ref(tracker, private_ref, candidate.commit_oid)
    raise AssertionError("completion push loop exhausted without an outcome")


def deliver_candidate(
    candidate: CompletionCandidate,
    tracker: str,
    ticket_id: str,
    basis: CompletionReadBasis,
    repo_root,
    equivalent_close: EquivalentClose,
) -> DeliveryResult:
    """Publish one private candidate or discard it without contaminating shared HEAD.

    A non-fast-forward rejection is followed by an ordinary fetch and receipt replay.  A
    relevant delta raises before any shared local close exists; an unrelated delta is merged
    and asks the caller to rebuild the candidate on the new base.  No force ref update exists
    on this path.
    """
    from rebar._store import push

    if candidate.commit_oid is None:
        raise TypeError("completion candidate has no commit")
    started = time.monotonic_ns()
    root = str(config.repo_root(repo_root))
    mode = push._push_mode(root)
    if mode != "always":
        raise _concurrency(
            "Error: sync.push changed after the pinned completion run selected atomic "
            f"delivery (now {mode!r}); retry the close"
        )
    try:
        branch = config.tickets_branch(root)
        remote = config.tickets_remote(root)
    except config.ConfigError as exc:
        raise CommandError(f"Error: invalid completion delivery destination: {exc}") from None
    remote_url = push._git(tracker, "remote", "get-url", remote)
    if remote_url.returncode != 0:
        return _deliver_local(
            candidate,
            tracker,
            ticket_id,
            basis,
            equivalent_close,
            started_ns=started,
        )
    try:
        push._require_s3_helper_if_s3_url(remote_url.stdout.strip())
    except Exception as exc:  # noqa: BLE001 -- the compatibility helper has no typed error
        raise CommandError(f"Error: completion delivery preflight failed: {exc}") from None
    current = tracker_head(tracker)
    if current != candidate.base_oid:
        validation, validation_ms = _validate(tracker, basis, current)
        if not validation.valid:
            if equivalent_close(tracker, ticket_id, basis, current):
                return DeliveryResult(
                    "already_present",
                    _metrics(
                        started,
                        push_attempts=0,
                        receipt_validation_ms=validation_ms,
                    ),
                    current,
                    idempotent=True,
                )
            raise _receipt_conflict(validation, ticket_id)
        return DeliveryResult(
            "retry_local_advance",
            _metrics(
                started,
                push_attempts=0,
                receipt_validation_ms=validation_ms,
            ),
            candidate.commit_oid,
            retry=True,
        )
    return _push_remote_candidate(
        candidate,
        tracker,
        ticket_id,
        basis,
        remote,
        branch,
        equivalent_close,
        started_ns=started,
    )


__all__ = ["DeliveryResult", "deliver_candidate"]
