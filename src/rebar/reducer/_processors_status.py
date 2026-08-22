"""STATUS-event processing for the ticket reducer (split from ``_processors`` — ticket ce02).

Extracted verbatim from :mod:`rebar.reducer._processors`: the ``process_status`` fold and its
three exclusive helpers (`_fold_plan_review_phase`, `_fold_claimed_session`,
`_fold_close_metadata`). This module imports nothing from ``_processors`` so the split stays
one-way; ``_processors`` re-exports these names for back-compat (the ``_processors_identity``
precedent).
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
    """Record the claiming session id on a winning ``open -> in_progress`` STATUS fold.

    Epic crust-fetch-stump, story 68ef. ``claimed_session`` records the session that
    performed the CURRENT ``open -> in_progress`` claim (mirrors ``assignee``). On that
    edge we set it to ``data.get("session")`` — the id the write side stamped, or ``None``
    when the claim carried no session. Setting to ``None`` on a session-less re-claim
    deliberately CLEARS any stale prior id, so the field never mis-attributes the current
    in_progress episode to a past session (advisory T9). Keyed on the ``open -> in_progress``
    edge only, so a later ``blocked -> in_progress`` (etc.) leaves it untouched.

    It is applied ONLY when this event's status is being applied — the caller invokes it in
    the normal-update and fork-WINNER branches, never where the existing chain wins — so a
    losing concurrent claim never overwrites the winner's session (advisory G6/T8). Older
    clones ignore the additive ``data["session"]`` key (forward-compatible).
    """
    if data.get("current_status") == "open" and data.get("status") == "in_progress":
        state["claimed_session"] = data.get("session")
        # Multi-harness provenance (story c557): fold the harness tag and secondary remote
        # session on the same edge, with the same fork-winner + session-less-clear semantics.
        state["claim_harness"] = data.get("harness")
        state["claim_remote_session"] = data.get("remote_session")


def _fold_close_metadata(state: dict, data: dict) -> None:
    """Record the close metadata carried on a winning ``*->closed`` STATUS fold (ticket ed13;
    extended with ``force_close_reason`` by bug defiant-orthoclase-buck).

    ``close_class`` records WHY a bug closed — the bounded ``--class`` enum the close write
    stamped onto the ``*->closed`` STATUS event's ``data`` (see
    ``rebar._commands.txn.transition_core``). On that edge we set it ONLY when the write side
    actually stamped a value — a non-bug close or an ``idea->closed`` drop carries none, and we
    leave the key ABSENT rather than storing ``None`` (present-only, so the optional
    string-enum ``close_class`` schema is never handed a null). Mirrors
    :func:`_fold_claimed_session`: applied ONLY when THIS event's status is being applied (the
    caller invokes it in the normal-update and fork-WINNER branches, NEVER where the existing
    chain wins), so a losing concurrent close never overwrites the winner's class. Older clones
    ignore the additive ``data["close_class"]`` key (forward-compatible)."""
    if data.get("status") == "closed" and data.get("close_class"):
        state["close_class"] = data["close_class"]
    # The `--force=<reason>` bypass reason travels with the same edge, under the same
    # present-only rule and the same winner-only application, so a losing concurrent close
    # cannot overwrite the winner's reason (bug defiant-orthoclase-buck). Folded here rather
    # than in its own function because it is the same datum shape on the same edge; splitting
    # would duplicate the winner-only call sites for no gain.
    if data.get("status") == "closed" and data.get("force_close_reason"):
        state["force_close_reason"] = data["force_close_reason"]
    # The reason-only administrative disposition's justification (`--class obsolete/wontfix
    # --reason=<text>`, ticket fc20) travels with the same edge, under the same present-only
    # rule and the same winner-only application. Distinct from `force_close_reason` above:
    # this records why a truthful disposition closed, that one records why a gate was bypassed.
    if data.get("status") == "closed" and data.get("close_reason"):
        state["close_reason"] = data["close_reason"]
    # WHY a completion signature was or was not expected for this close (story
    # mechanical-coherent-wolverine) travels with the same edge, under the same present-only
    # rule and the same winner-only application. Absent on a historical close event means
    # unknown/legacy — the key stays ABSENT in reduced state, never guessed.
    if data.get("status") == "closed" and data.get("completion_expectation"):
        state["completion_expectation"] = data["completion_expectation"]


def process_status(state: dict, event: dict, data: dict, _filepath: str) -> None:
    """Apply a STATUS event with fork detection and lexical UUID tie-break.

    If current_status in the event doesn't match state['status'], a fork has
    been detected (two competing chains diverged). Resolve by comparing the
    incoming event's own UUID (``event.get("uuid")``) against the UUID already
    recorded in ``state['parent_status_uuid']`` — the lexically lower UUID
    wins and its target_status is applied. Using event-own UUIDs (not parent
    pointers) makes concurrent siblings with the same parent resolve
    deterministically regardless of replay order (bug 1587-4816).

    On a normal (non-fork) update, ``state['status']`` is updated to the
    event's target status and ``state['parent_status_uuid']`` is advanced to
    the event's own UUID so subsequent forks compare against the winner's
    identity, not its parent.

    The legacy ``state['conflicts']`` key is never written and is removed if
    found (e.g. replayed from an old SNAPSHOT compiled_state).
    """
    # Remove legacy conflicts key unconditionally — new behavior never uses it.
    state.pop("conflicts", None)

    # Capture the pre-update status so we can detect a closed->open reopen below.
    prev_status = state.get("status")

    current_status = data.get("current_status")
    if current_status is not None and current_status != state["status"]:
        # Fork detected: two chains have diverged.
        #
        # Tie-break uses the events' own UUIDs (not parent pointers) so that
        # concurrent siblings — two STATUS events with the same parent pointer —
        # resolve deterministically regardless of replay order (bug 1587-4816).
        # state["parent_status_uuid"] is advanced to the WINNING event's own UUID
        # so subsequent forks compare against the winner's identity, not its parent.
        incoming_uuid = event.get("uuid") or ""
        existing_uuid = state.get("parent_status_uuid") or ""

        # Lower lexical UUID wins. Empty existing_uuid means no prior fork
        # winner has been recorded, so the incoming event wins unconditionally
        # (otherwise any non-empty incoming UUID > "" and the existing-empty
        # branch would always win, leaving state.status stuck at the loser's
        # value — bug e60b-e698, regression test:
        # test_reducer_applies_multiple_status_events_current_status_mismatch_resolves_fork).
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
        # Record the resolved fork in PURE derived state (rebuilt identically on every
        # replay — no external I/O) so a concurrent claim/status race becomes discoverable
        # via fsck/show after the fact (audit reliability #1, story 3003). loser_env_id is
        # intentionally NOT stored: it is unreliable across reopens, and claim-loss
        # detection uses the authoritative `assignee` field instead (see _commands/claim).
        state.setdefault("status_fork_resolutions", []).append(
            {"winner_uuid": winner_uuid, "dropped_uuid": loser_uuid}
        )
    else:
        state["status"] = data.get("status", state["status"])
        _fold_plan_review_phase(state, state["status"])
        _fold_claimed_session(state, data)  # normal (non-fork) update — this event is applied
        _fold_close_metadata(state, data)  # close metadata on the *->closed edge
        # Advance to THIS event's OWN UUID (not its data parent-pointer) so a
        # subsequent concurrent sibling forks against this event's identity and
        # resolves by the lexical-UUID rule above — deterministically and
        # independent of replay order, exactly as this docstring / docs/concurrency.md
        # describe. Bug 8874: the previous `data["parent_status_uuid"]` stored the
        # common-parent pointer, so two siblings from an EMPTY parent compared the
        # incoming uuid against "" and the later-replayed event won by insertion
        # order rather than by UUID. Matches the fork branch above, which already
        # records the winner's own UUID.
        state["parent_status_uuid"] = event.get("uuid") or ""
        state["last_status_env_id"] = event.get("env_id") or ""

    # Record the most recent closed->open (reopen) transition timestamp (epic
    # dark-acme-lumen). Validity-on-read uses it to invalidate a completion/plan-review
    # attestation signed BEFORE a reopen, without mutating the immutable attestation record.
    # Set only on the closed->open edge (applies to both the fork and normal branches via the
    # resolved status); left absent for tickets that were never reopened.
    if prev_status == "closed" and state.get("status") == "open":
        state["last_reopened_at"] = event.get("timestamp")
