"""Immutable plan-review generation and atomic pre-sign validation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from rebar import config

from .relation_snapshot import (
    PlanRelationSnapshot,
    PlanRelationSnapshotError,
    collect_plan_relation_snapshot,
    tracker_head_sha,
)

logger = logging.getLogger(__name__)
EXECUTION_PRIORITY_FLOOR = 0.80
MAX_GENERATION_ATTEMPTS = 3


@dataclass(frozen=True)
class PlanReviewGeneration:
    phase: Literal["planning", "execution"]
    priority_floor: float | None
    own_material: str
    relation_snapshot: PlanRelationSnapshot
    ticket_store_revision: str


class PlanReviewGenerationError(RuntimeError):
    """Base class for structured unsigned signing outcomes."""

    retryable = False
    event = "plan_review_sign_aborted"


class PlanReviewGenerationChanged(PlanReviewGenerationError):
    event = "plan_review_generation_changed"


class PlanReviewGenerationRetryable(PlanReviewGenerationError):
    retryable = True
    event = "plan_review_generation_retry"


class _UnderLockMismatch(RuntimeError):
    pass


def _phase_for_state(state: dict) -> Literal["planning", "execution"]:
    phase = state.get("plan_review_phase")
    if phase in ("planning", "execution"):
        return phase
    return "planning" if state.get("status") in (None, "open", "idea") else "execution"


def from_snapshot(snapshot: PlanRelationSnapshot) -> PlanReviewGeneration:
    """Derive every signed generation field from one exact relation snapshot."""
    from .det_floor import PlanContext
    from .pass1 import material_fingerprint

    state = snapshot.subject_state
    phase = _phase_for_state(state)
    ctx = PlanContext(
        ticket_id=state.get("ticket_id", ""),
        ticket_type=state.get("ticket_type", ""),
        title=state.get("title", ""),
        description=state.get("description", ""),
        state=state,
        children=[{"ticket_id": child_id} for child_id in snapshot.child_ids],
    )
    return PlanReviewGeneration(
        phase=phase,
        priority_floor=EXECUTION_PRIORITY_FLOOR if phase == "execution" else None,
        own_material=material_fingerprint(ctx),
        relation_snapshot=snapshot,
        ticket_store_revision=snapshot.ticket_store_revision,
    )


def collect(
    ticket_id: str, *, repo_root=None, ignore_untracked: bool = False
) -> PlanReviewGeneration:
    return from_snapshot(
        collect_plan_relation_snapshot(
            ticket_id, repo_root=repo_root, ignore_untracked=ignore_untracked
        )
    )


def _log(level: int, event: str, **fields) -> None:
    record = {"event": event, **fields}
    logger.log(level, "%s: %s", event, record, extra=record)


def sign_manifest(
    ticket_id: str,
    manifest: list[str],
    initial_generation: PlanReviewGeneration,
    *,
    repo_root=None,
) -> dict:
    """Sign only if one stable generation still equals the immutable initial baseline."""
    from rebar import signing
    from rebar._store.lock import LockTimeout

    tracker = str(config.tracker_dir(repo_root))
    for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
        try:
            # Ignore UNTRACKED tracker files here, consistently with the authoritative
            # under-lock re-check below (``under_lock_check``): the fence detects a
            # concurrent COMMIT during generation (a moving committed HEAD), which
            # untracked files cannot cause. Treating an unrelated crashed process's stray
            # artifact in the SHARED tracker as fatal would abort signing (no durable
            # attestation → the claim gate cannot pass) for a clean plan (bug d7cb-22ae).
            before = tracker_head_sha(tracker, ignore_untracked=True)
            fresh = collect(ticket_id, repo_root=repo_root, ignore_untracked=True)
            after = tracker_head_sha(tracker, ignore_untracked=True)
        except PlanRelationSnapshotError as exc:
            _log(logging.ERROR, "plan_review_sign_aborted", reason=exc.reason, attempt=attempt)
            raise PlanReviewGenerationError(exc.reason) from None
        if before != after:
            _log(
                logging.WARNING,
                "plan_review_generation_retry",
                attempt=attempt,
                before=before,
                after=after,
            )
            continue
        if fresh != initial_generation:
            _log(
                logging.WARNING,
                "plan_review_generation_changed",
                attempt=attempt,
                before=before,
                after=after,
            )
            raise PlanReviewGenerationChanged("plan review generation changed; re-review required")

        def under_lock_check(expected_after=after) -> None:
            locked_head = tracker_head_sha(tracker, ignore_untracked=True)
            locked_generation = collect(ticket_id, repo_root=repo_root, ignore_untracked=True)
            if locked_head != expected_after or locked_generation != initial_generation:
                raise _UnderLockMismatch

        try:
            return signing._sign_manifest_under_lock(
                ticket_id,
                manifest,
                kind="plan-review",
                repo_root=repo_root,
                under_lock_check=under_lock_check,
            )
        except _UnderLockMismatch:
            _log(
                logging.WARNING,
                "plan_review_generation_retry",
                attempt=attempt,
                before=after,
                after="under-lock-mismatch",
            )
        except LockTimeout as exc:
            _log(logging.WARNING, "plan_review_generation_retry", attempt=attempt, reason="lock")
            raise PlanReviewGenerationRetryable(str(exc)) from None
        except PlanReviewGenerationError:
            raise
        except Exception as exc:  # noqa: BLE001 - terminal signing failures become unsigned
            _log(
                logging.ERROR,
                "plan_review_sign_aborted",
                attempt=attempt,
                reason=type(exc).__name__,
            )
            raise PlanReviewGenerationError(str(exc)) from None
    raise PlanReviewGenerationRetryable("plan review generation remained unstable after 3 attempts")
