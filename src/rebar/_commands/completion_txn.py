"""Atomic committed-store transaction for a certified completion close.

The billable verifier and signature mint run before this module is entered. Its critical
section is intentionally narrow: compare the committed tracker OID, re-read the ticket and
existing close guards, compose the STATUS event, then build the verdict + STATUS + op-cert
as one private candidate commit. It never pushes while holding the ticket-store write lock.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from rebar._commands import _seam, completion_candidate, txn
from rebar._commands._seam import CommandError
from rebar._snapshot.ticket_view import TicketsOID, tracker_head
from rebar._store import event_append, hlc


class TrackerHeadAdvanced(txn.ConcurrencyMismatch):
    """The tracker changed after receipt validation but before lock acquisition."""

    def __init__(self, message: str, metrics: Mapping[str, int] | None = None) -> None:
        super().__init__(message)
        self.metrics = metrics or {}


@dataclass(frozen=True)
class AtomicCloseCommit:
    commit_oid: TicketsOID
    candidate: completion_candidate.CompletionCandidate
    metrics: Mapping[str, int]


def _finalize_prepared_event(
    tracker: str,
    ticket_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    *,
    timestamp: int,
    event_uuid: str,
    env_id: str,
    author: str,
    repo_root,
) -> dict[str, Any]:
    """Finalize one precomputed payload after its final HLC position is known."""
    data = copy.deepcopy(dict(payload))
    event = {
        "timestamp": timestamp,
        "uuid": event_uuid,
        "event_type": event_type,
        "env_id": env_id,
        "author": author,
        "data": data,
    }
    _seam.finalize_event(event, ticket_id, event_type, data, tracker, repo_root)
    return event


def commit_atomic_completion_close(
    tracker: str,
    ticket_id: str,
    *,
    expected_tickets_oid: TicketsOID,
    verdict_payload: Mapping[str, Any],
    signature_payload: Mapping[str, Any],
    verdict_uuid: str,
    status_uuid: str,
    signature_uuid: str,
    run_id: str,
    env_id: str,
    author: str,
    close_class: str = "",
    close_reason: str = "",
    completion_expectation: str = "required",
    repo_root=None,
    pre_status_check: Callable[[Mapping[str, Any]], None] | None = None,
) -> AtomicCloseCommit:
    """Build one isolated verdict + STATUS + signature candidate under the store lock.

    The caller validates the read receipt immediately before each attempt and supplies the
    exact tracker OID it validated.  A concurrent committed write changes HEAD and produces
    :class:`TrackerHeadAdvanced`; the caller may revalidate and retry.  No event is visible on
    the shared tracker on any validation, signing, staging, index, commit, or later delivery
    failure.  The returned candidate must be delivered or discarded by the caller.
    """
    if not isinstance(expected_tickets_oid, TicketsOID):
        raise TypeError("expected_tickets_oid must be a TicketsOID")
    candidate_started = time.monotonic_ns()
    try:
        candidate = completion_candidate.prepare_candidate(
            tracker,
            expected_tickets_oid,
            ticket_id,
            run_id=run_id,
        )
    except completion_candidate.CandidateError as exc:
        raise CommandError(
            f"Error: could not prepare isolated completion candidate: {exc}", returncode=1
        ) from None
    candidate_prepare_ms = (time.monotonic_ns() - candidate_started) // 1_000_000
    wait_started = time.monotonic_ns()
    try:
        handle = txn._acquire_write_lock(tracker)
    except BaseException:
        candidate.cleanup()
        raise
    lock_acquired = time.monotonic_ns()
    metrics = {
        "atomic_close_lock_wait_ms": (lock_acquired - wait_started) // 1_000_000,
        "atomic_close_lock_hold_ms": 0,
        "atomic_close_commit_ms": 0,
        "atomic_close_events": 3,
        "atomic_close_candidate_prepare_ms": candidate_prepare_ms,
    }
    succeeded = False
    try:
        current = tracker_head(tracker)
        if current != expected_tickets_oid:
            raise TrackerHeadAdvanced(
                "Error: ticket store advanced after completion receipt validation; "
                "revalidating before close",
                metrics,
            )
        # Final ordering belongs to the committed-store transaction.  The expensive verdict
        # normalization and op-cert mint have already happened, but their event envelopes do
        # not receive HLC positions or authorship signatures until the final tracker OID and
        # ticket status have been checked under the write lock.
        verdict_timestamp = hlc.next_tick(tracker, ticket_id)
        status_timestamp = hlc.next_tick(tracker, ticket_id)
        signature_timestamp = hlc.next_tick(tracker, ticket_id)
        verdict_event = _finalize_prepared_event(
            tracker,
            ticket_id,
            "COMPLETION_VERDICT",
            verdict_payload,
            timestamp=verdict_timestamp,
            event_uuid=verdict_uuid,
            env_id=env_id,
            author=author,
            repo_root=repo_root,
        )
        status_event = txn.prepare_transition_event_locked(
            tracker,
            ticket_id,
            "in_progress",
            "closed",
            env_id=env_id,
            author=author,
            close_class=close_class,
            close_reason=close_reason,
            completion_expectation=completion_expectation,
            repo_root=repo_root,
            pre_status_check=pre_status_check,
            timestamp=status_timestamp,
            event_uuid=status_uuid,
        )
        signature_event = _finalize_prepared_event(
            tracker,
            ticket_id,
            "SIGNATURE",
            signature_payload,
            timestamp=signature_timestamp,
            event_uuid=signature_uuid,
            env_id=env_id,
            author=author,
            repo_root=repo_root,
        )
        commit_started = time.monotonic_ns()
        if tracker_head(candidate.tracker) != expected_tickets_oid:
            raise completion_candidate.CandidateError(
                "isolated completion candidate does not match its validated tracker base"
            )
        # The candidate is operation-private, so ownership is stronger than a lock: no other
        # process has its path or index. Reuse the existing-lock batch seam to preserve the
        # canonical staging, fault rollback, and one-commit contracts without taking a second
        # unrelated store lock.
        event_append.batch_stage_and_commit_under_lock(
            candidate.tracker,
            [
                (ticket_id, verdict_event),
                (ticket_id, status_event),
                (ticket_id, signature_event),
            ],
            commit_msg=f"ticket: certified close {ticket_id}",
        )
        metrics["atomic_close_commit_ms"] = (time.monotonic_ns() - commit_started) // 1_000_000
        committed = tracker_head(candidate.tracker)
        candidate = candidate.with_commit(committed)
        succeeded = True
    except CommandError:
        raise
    except (event_append.StoreError, event_append.RebaseGuard, event_append.LockTimeout) as exc:
        raise CommandError(str(exc), returncode=getattr(exc, "returncode", 1)) from None
    except Exception as exc:  # noqa: BLE001 — fail closed at the transaction boundary
        raise CommandError(f"Error: atomic completion close failed: {exc}", returncode=1) from None
    finally:
        metrics["atomic_close_lock_hold_ms"] = (time.monotonic_ns() - lock_acquired) // 1_000_000
        handle.release()
        if not succeeded:
            candidate.cleanup()
    return AtomicCloseCommit(committed, candidate, metrics)


__all__ = [
    "AtomicCloseCommit",
    "TrackerHeadAdvanced",
    "commit_atomic_completion_close",
]
