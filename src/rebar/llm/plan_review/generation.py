"""Immutable plan-review generation and atomic pre-sign validation."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal

from rebar import config

from .relation_snapshot import (
    PlanMaterialPin,
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
    """Immutable identity of the plan material a review was based on.

    Equality is deliberately scoped to exactly what the signed manifest binds: the subject's
    own material, its DIRECT related material (child/prerequisite pins), and the phase/floor.
    ``relation_snapshot`` and ``ticket_store_revision`` are carried for downstream readers but are
    NOT part of identity (``compare=False``) — they are store-WIDE (the full ticket-states map and
    the tracker HEAD), so including them made an unrelated ticket's concurrent write abort signing
    even though the signed artifact was unchanged (client report §2).
    """

    phase: Literal["planning", "execution"]
    priority_floor: float | None
    own_material: str
    relation_snapshot: PlanRelationSnapshot = field(compare=False)
    ticket_store_revision: str = field(compare=False)
    related_material: tuple[PlanMaterialPin, ...] = ()


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


def _related_material_delta(fresh, initial) -> str:
    """Name WHICH dependency moved. ``resign`` already names the ids; a bare "a dependency"
    left the reader to re-derive the pin set by hand (bug 94a3). An id present on one side
    only was added/removed; otherwise the id whose pinned fingerprint changed."""
    added_or_removed = sorted({p.canonical_id for p in fresh} ^ {p.canonical_id for p in initial})
    changed = added_or_removed or sorted(p.canonical_id for p in fresh if p not in initial)
    return ", ".join(changed) if changed else "unknown"


def _own_material_delta(ticket_id: str, initial_generation, repo_root) -> str:
    """Name the material component that moved mid-review (bug 94a3). The reviewed state is
    still in hand as ``initial_generation.relation_snapshot``, so this is an exact diff, not
    a guess. Best-effort: an unreadable side degrades to a neutral clause."""
    from .material_diff import explain_snapshot_change

    try:
        return explain_snapshot_change(
            initial_generation.relation_snapshot, ticket_id, repo_root=repo_root
        )
    except Exception:  # noqa: BLE001 — a diagnostic clause must never mask the real abort
        return "the changed component could not be determined"


# ── mid-run cancellation on OWN-material change (story 2c89) ──────────────────
#
# A plan edited mid-review used to run every remaining pass and only fail at the
# sign-time re-check below — pure waste, since the Pass-1 checkpoints are keyed by
# the material fingerprint and are already orphaned by the edit. The cancel
# predicate is scoped EXACTLY like the sign-time one: the subject's OWN material
# only. It never reads tracker HEAD, the relation snapshot, or related_material —
# store-wide equality false-cancels on unrelated tickets' writes (bug d70a), and
# related-material drift keeps reusable checkpoints and is still
# refused at sign time. Monotone by construction: a cancel can only WITHHOLD an
# attestation (the cancelled verdict is unsigned INDETERMINATE); a missed cancel
# degrades to today's sign-time refusal.


class PlanReviewCancelledStale(RuntimeError):
    """Raised at a between-pass seam when the subject's OWN material changed mid-run."""


@dataclass
class _CancelScope:
    """The run-scoped cancel token: one per ``produce_plan_review_verdict`` run."""

    ticket_id: str
    baseline_own_material: str | None
    repo_root: object = None
    event: threading.Event = field(default_factory=threading.Event)
    seam: str | None = None  # which probe fired (observability; None until cancelled)


_CANCEL_SCOPE: ContextVar[_CancelScope | None] = ContextVar(
    "plan_review_cancel_scope", default=None
)


@contextmanager
def cancel_scope(
    ticket_id: str, baseline_own_material: str | None, *, repo_root=None
) -> Iterator[_CancelScope]:
    """Install the run-scoped cancel token. ContextVar-based so the seam probes (workflow
    ops) and the Pass-1 pool workers (which inherit a ``copy_context()`` via
    ``pass1._submit_ctx``) all see the same scope without parameter threading."""
    scope = _CancelScope(
        ticket_id=ticket_id, baseline_own_material=baseline_own_material, repo_root=repo_root
    )
    token = _CANCEL_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _CANCEL_SCOPE.reset(token)


def review_cancelled() -> bool:
    """Whether the active review run (if any) has been cancelled. Checked at the top of
    the Pass-1 ``_chunk`` funnel so a not-yet-started chunk never reaches the runner."""
    scope = _CANCEL_SCOPE.get()
    return scope is not None and scope.event.is_set()


def own_material_changed(ticket_id: str, baseline: str | None, *, repo_root=None) -> bool:
    """Has the ticket's OWN material fingerprint moved off ``baseline``?

    A single-ticket light read (:func:`attest.current_material_fingerprint`: the ticket +
    its child ids — no store-wide reduction) wrapped in ``local_read_context`` so the
    probe never triggers a fetch/reconverge or contends for the sync lock. Fail-open:
    an unknown fingerprint (read error / deleted) is ``False`` — never cancel on doubt;
    the sign-time re-check stays authoritative."""
    if not baseline:
        return False
    from rebar._engine_support import reads as ticket_reads

    from . import attest

    with ticket_reads.local_read_context():
        current = attest.current_material_fingerprint(ticket_id, repo_root=repo_root)
    return current is not None and current != baseline


def probe_cancel(seam: str) -> None:
    """The between-pass seam probe: no active scope → no-op; a set event or a changed
    OWN material → set the event and raise :class:`PlanReviewCancelledStale` (the
    interpreter captures it as a failed step and short-circuits the remaining passes;
    ``produce_plan_review_verdict`` reads the event and returns the cancelled verdict
    BEFORE its recovery reconstructions)."""
    scope = _CANCEL_SCOPE.get()
    if scope is None:
        return
    if scope.event.is_set():
        raise PlanReviewCancelledStale(
            f"plan review of {scope.ticket_id} already cancelled (own material changed)"
        )
    if own_material_changed(
        scope.ticket_id, scope.baseline_own_material, repo_root=scope.repo_root
    ):
        scope.seam = seam
        scope.event.set()
        _log(
            logging.WARNING,
            "plan_review_cancelled_stale",
            ticket_id=scope.ticket_id,
            seam=seam,
        )
        raise PlanReviewCancelledStale(
            f"the OWN plan material of {scope.ticket_id} changed mid-review "
            f"(detected at the {seam} seam); the remaining passes are skipped"
        )


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
        related_material=snapshot.related_material,
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
            if fresh.own_material != initial_generation.own_material:
                message = (
                    "the reviewed ticket's own plan material changed mid-review — "
                    f"{_own_material_delta(ticket_id, initial_generation, repo_root)}"
                    "; re-review required"
                )
            elif fresh.related_material != initial_generation.related_material:
                named = _related_material_delta(
                    fresh.related_material, initial_generation.related_material
                )
                message = (
                    f"a dependency's plan material changed since the review ({named}); "
                    "re-review required"
                )
            elif (
                fresh.phase != initial_generation.phase
                or fresh.priority_floor != initial_generation.priority_floor
            ):
                message = "the plan review phase changed since the review; re-review required"
            else:
                message = "the plan review generation changed since the review; re-review required"
            raise PlanReviewGenerationChanged(message)

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
