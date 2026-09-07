"""Status folding without an import cycle.

This module owns status reduction. ``_processors`` reexports its helpers for compatibility.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _fold_plan_review_phase(state: dict, target_status: object) -> None:
    """Project only winning planning/execution lifecycle edges."""
    if target_status == "in_progress":
        state["plan_review_phase"] = "execution"
    elif target_status == "open":
        state["plan_review_phase"] = "planning"


def _fold_claimed_session(state: dict, data: dict) -> None:
    """Fold claim provenance only for the winning ``open -> in_progress`` event.

    Values from the winner replace prior session, harness, and remote session fields. Missing
    values clear stale claim provenance. Other transitions do not alter them.
    """
    if data.get("current_status") == "open" and data.get("status") == "in_progress":
        state["claimed_session"] = data.get("session")
        # Multi-harness provenance (story c557): fold the harness tag and secondary remote
        # session on the same edge, with the same fork-winner + session-less-clear semantics.
        state["claim_harness"] = data.get("harness")
        state["claim_remote_session"] = data.get("remote_session")


def _clear_claimed_session(state: dict, data: dict) -> None:
    """Clear claim provenance only when the winning event exits ``in_progress``.

    Entry and same-state transitions retain current values. A losing fork never reaches this
    helper.
    """
    if data.get("current_status") == "in_progress" and data.get("status") != "in_progress":
        state["claimed_session"] = None
        state["claim_harness"] = None
        state["claim_remote_session"] = None


def _fold_close_metadata(state: dict, data: dict) -> None:
    """Fold nonempty close metadata only for the winning close event.

    An absent ``close_class`` remains absent. Callers apply this helper only to normal or
    fork-winning events.
    """
    if data.get("status") == "closed" and data.get("close_class"):
        state["close_class"] = data["close_class"]
    # Fold the gate bypass reason at the same winner-only call site.
    if data.get("status") == "closed" and data.get("force_close_reason"):
        state["force_close_reason"] = data["force_close_reason"]
    # ``close_reason`` describes an administrative disposition, not a gate bypass.
    if data.get("status") == "closed" and data.get("close_reason"):
        state["close_reason"] = data["close_reason"]
    # Preserve an explicit completion expectation. Absence remains unknown.
    if data.get("status") == "closed" and data.get("completion_expectation"):
        state["completion_expectation"] = data["completion_expectation"]


def process_status(state: dict, event: dict, data: dict, _filepath: str) -> None:
    """Fold STATUS events with lexical UUID convergence.

    A mismatched current status forms a fork. The smaller event UUID wins, which makes replay
    order irrelevant. Normal events set the target status and their own UUID. Legacy
    ``conflicts`` state is removed.
    """
    # Remove legacy conflicts key unconditionally — new behavior never uses it.
    state.pop("conflicts", None)

    # Capture the pre-update status so we can detect a closed->open reopen below.
    prev_status = state.get("status")

    current_status = data.get("current_status")
    if current_status is not None and current_status != state["status"]:
        # Compare sibling event UUIDs. Persist the winner's UUID for later fork comparisons.
        incoming_uuid = event.get("uuid") or ""
        existing_uuid = state.get("parent_status_uuid") or ""

        # The incoming event wins when no prior winner UUID exists. Comparing against an empty
        # UUID would retain the losing status.
        if not existing_uuid or incoming_uuid <= existing_uuid:
            # Incoming event wins.
            winner_uuid = incoming_uuid
            loser_uuid = existing_uuid
            # Use last_status_env_id (set by most recent STATUS event) so we log
            # the losing STATUS author's env, not the ticket creator's env.
            loser_env_id = state.get("last_status_env_id") or ""
            state["status"] = data.get("status", state["status"])
            _fold_plan_review_phase(state, state["status"])
            state["parent_status_uuid"] = incoming_uuid  # winner's own UUID
            _fold_claimed_session(state, data)  # only when THIS (winning) event is applied
            _clear_claimed_session(state, data)  # clear provenance on the winning in_progress exit
            _fold_close_metadata(state, data)  # close metadata on the *->closed winning edge
        else:
            # Existing chain wins; keep state as-is.
            winner_uuid = existing_uuid
            loser_uuid = incoming_uuid
            loser_env_id = event.get("env_id", "") or ""

        ticket_id = state.get("ticket_id", "")
        logger.warning(
            "PARENT_CHAIN_FORK_RESOLVED ticket=%s winner=%s dropped=[%s] loser_env_id=[%s]",
            ticket_id,
            winner_uuid,
            loser_uuid,
            loser_env_id,
        )
        # Record deterministic fork evidence without I/O. ``loser_env_id`` becomes unreliable
        # after reopen, while claim-loss checks use the assignee.
        state.setdefault("status_fork_resolutions", []).append(
            {"winner_uuid": winner_uuid, "dropped_uuid": loser_uuid}
        )
    else:
        state["status"] = data.get("status", state["status"])
        _fold_plan_review_phase(state, state["status"])
        _fold_claimed_session(state, data)  # normal (non-fork) update — this event is applied
        _clear_claimed_session(state, data)  # clear provenance on the in_progress exit
        _fold_close_metadata(state, data)  # close metadata on the *->closed edge
        # Store this UUID for later sibling comparisons. A common-parent UUID would make an
        # empty-parent fork depend on replay order.
        state["parent_status_uuid"] = event.get("uuid") or ""
        state["last_status_env_id"] = event.get("env_id") or ""

    # Record the newest winning reopen time. Read validation uses it to invalidate older
    # attestations, while tickets never reopened omit the field.
    if prev_status == "closed" and state.get("status") == "open":
        state["last_reopened_at"] = event.get("timestamp")
