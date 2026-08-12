"""Contributor-triggerable re-review — the ``rerun-llm-review`` comment trigger (ticket bb9b).

WHAT THIS IS. The review-bot's fail-closed design leaves two states a CONTRIBUTOR could
previously only escape by finding an operator: a coverage-gap ``-1`` whose underlying
infra failure has since been corrected, and a retries-exhausted escalation (ticket 0347)
whose bounded automatic recovery has been spent. This module lets the contributor comment
``rerun-llm-review`` on the Gerrit change to request a FRESH review through the same
fail-closed pipeline — the human analogue of the reconciler's re-drive, and the
contributor analogue of the operator's ``/rerun``.

SECURITY MODEL (must hold — tested, not just implemented):

* Eligibility is decided PRIVILEGED-SIDE from durable Gerrit state (the bot's own vote
  message tag on the CURRENT revision), never from anything the requesting comment says.
* The ONLY ineligible state is a current-revision findings-BLOCK (a real code veto):
  the fix for a finding is amend + re-push, not a retry. Everything else — every
  coverage-gap sub-reason, a retries-exhausted escalation, a PASS, a vote-less change,
  even an unparseable tag — is eligible, because a re-review is always safe: it can only
  produce a fresh fail-closed verdict, never mint an unearned PASS.
* Only AFTER eligibility succeeds may this path call the distinct privileged reset
  primitive. Its label value is fixed at exactly neutral ``LLM-Review: 0``; contributor
  text cannot choose a value, mint a PASS, or create a findings-BLOCK. The reset is then
  verified with the all-accounts vote reader before acceptance.
* Affirmative/refusal replies still go through :meth:`GerritClient.post_comment`, whose
  request body has NO ``labels`` key at all. Ineligible, malformed, and failed-eligibility
  requests perform no label write.
* The bot's own comments never trigger (loop guard), so the reply cannot re-trigger.

On acceptance the exact revision's vote is reset and verified first, then the bot
best-effort re-arms the 0347 retry budget (``reset_attempts`` — the row DELETE) before
the forced re-review is enqueued. A local budget-reset failure is logged but cannot
reject a request after its authoritative Gerrit marker already exists. The reset's
Gerrit event is the durable recovery marker if the process-local queue is lost before
the worker consumes it.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from rebar.review_bot.config import ReceiverConfig
from rebar.review_bot.dedup import DedupStore
from rebar.review_bot.gerrit_client import GerritClient, GerritError

logger = logging.getLogger("rebar.review_bot.retrigger")

#: The magic comment token. Deliberately shares NO substring with CI's ``recheck`` ChatOps
#: word (``infra/gerrit/gerrit_to_platform.ini.template``, ``[mapping "comment-added"]``).
#: That external matcher is substring-based and we do not own it, so a trigger word that
#: EMBEDS ``recheck`` also re-dispatches the Verified workflow — which is how the retired
#: word cancelled in-flight CI runs (ticket 0d78-6c15-db26-4cea, changes 1551/1577).
TRIGGER_TOKEN = "rerun-llm-review"

#: Word-boundary match where ``-`` and ``_`` also bind: ``rerun-llm-review`` triggers,
#: ``rerun-llm-reviews`` / ``pre-rerun-llm-review`` / ``rerun-llm-review-x`` do not.
_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])rerun-llm-review(?![A-Za-z0-9_-])")

#: The RETIRED trigger word, kept ONLY so a contributor who types it gets told the new one.
#: Hard cutover — matching it never queues a review and never writes a label. This regex and
#: the docstring above are the sole surviving occurrences of the retired word in the package.
_RETIRED_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])recheck-review(?![A-Za-z0-9_-])")

#: A bot vote-message tag line: ``[LLM-Review: <body>]`` alone on its line. Gerrit
#: prepends a ``Patch Set N:``/``Patch Set N: LLM-Review-1`` header block to every
#: change message, so the tag is found by scanning lines, not by position 0.
_TAG_LINE_RE = re.compile(r"^\[LLM-Review: (?P<body>[^\]]*)\]\s*$")

#: The merge-change variant appends this INSIDE the tag brackets (adapter.py) —
#: e.g. ``[LLM-Review: BLOCK — finding (merge-change, 3 integrated commit(s))]``.
_MERGE_SUFFIX_RE = re.compile(r"\s*\(merge-change, \d+ integrated commit\(s\)\)$")

#: Coverage-gap stem: covers every ``_TAG_SUFFIXES`` gap member AND the hardcoded
#: ``merge-review`` literal emitted by ``voter._merge_coverage_gap_decision`` — that tag
#: is NOT a ``_TAG_SUFFIXES`` member, which is why this parser matches the grammar's
#: shape rather than enumerating the vocabulary.
_GAP_RE = re.compile(r"^BLOCK — coverage-gap \((?P<reason>[a-z][a-z-]*)\)$")


def _emit(level: int, event: str, **fields: Any) -> None:
    """One structured JSON log line, prefixed with the greppable event marker (the
    ``RETRIGGER_REFUSED`` / ``RETRIGGER_ACCEPTED`` markers are runbook-documented)."""
    record = {"event": event, "timestamp": time.time(), **fields}
    logger.log(level, "%s %s", event, json.dumps(record, default=str))


def classify_tag(message: str | None) -> str | None:
    """Classify the ``[LLM-Review: …]`` tag in one bot change message.

    Returns ``"pass"``, ``"finding"``, ``"coverage-gap:<reason>"``, ``"unparseable"``
    (a bracketed LLM-Review line that matches none of the known stems), or ``None``
    (no tag line at all). The in-tag merge-change suffix is stripped FIRST, so a merge
    finding — ``BLOCK — finding (merge-change, N integrated commit(s))`` — classifies
    as ``finding`` exactly like the bare form (the refusal must not be dodgeable by
    pushing a merge commit)."""
    for raw in (message or "").splitlines():
        m = _TAG_LINE_RE.match(raw.strip())
        if m is None:
            continue
        body = _MERGE_SUFFIX_RE.sub("", m.group("body").strip())
        if body == "PASS":
            return "pass"
        if body == "BLOCK — finding":
            return "finding"
        gap = _GAP_RE.match(body)
        if gap is not None:
            return f"coverage-gap:{gap.group('reason')}"
        return "unparseable"
    return None


#: The criterion whose remedy is a TICKET action, not a code change (ticket 6a81). Its finding is
#: "this bug has no acceptable plan-review attestation" — fixed by `rebar review-plan <id>`, so
#: `amend + re-push` is the wrong instruction and a no-code-change patchset only copies the -1
#: forward under the label's copy condition.
ATTESTATION_CRITERION = "bugfix-size-attestation"

#: The finding-summary HEADER, which states the TOTAL blocking count (`adapter.py:129`).
#: Load-bearing: the adapter renders only the first TEN findings as `- (...)` lines and clips each
#: detail at 240 chars, so the rendered LIST cannot establish "this was the only finding" — the
#: header can.
_HEADER_COUNT_RE = re.compile(r"^rebar code review found (?P<count>\d+) blocking issue\(s\):\s*$")

#: One rendered blocking-finding line: ``- (<criterion-id>) <detail>``.
_FINDING_LINE_RE = re.compile(r"^- \((?P<crit>[^)]+)\)")


def latest_bot_message(
    gerrit: GerritClient, change_id: str, current_patchset: int | None
) -> str | None:
    """The TEXT of the bot's latest tagged verdict message on the current revision, or ``None``.

    Same selection rule as :func:`latest_bot_tag_state` (current patchset, bot-authored tag, newest
    wins) — split out so the attestation arm can read the finding summary without re-implementing
    the scan and drifting from it."""
    text: str | None = None
    for msg in gerrit.get_change_messages(change_id):
        if not isinstance(msg, dict):
            continue
        if current_patchset is not None and msg.get("_revision_number") != current_patchset:
            continue
        if not str(msg.get("tag") or "").startswith("autogenerated:rebar"):
            continue
        if classify_tag(msg.get("message")) is not None:
            text = str(msg.get("message") or "")
    return text


def sole_blocking_criterion(message: str | None) -> str | None:
    """The criterion id of the ONLY blocking finding in ``message``, else ``None``.

    ``None`` whenever soleness cannot be established: no header, an unparseable count, a count that
    is not exactly 1, or no rendered finding line. FAIL-CLOSED by construction — a parse that cannot
    prove there was one finding is not evidence that there was."""
    lines = [raw.strip() for raw in (message or "").splitlines()]
    count: int | None = None
    for raw in lines:
        header = _HEADER_COUNT_RE.match(raw)
        if header is not None:
            try:
                count = int(header.group("count"))
            except ValueError:  # pragma: no cover — the regex admits only digits
                return None
            break
    if count != 1:
        return None
    for raw in lines:
        found = _FINDING_LINE_RE.match(raw)
        if found is not None:
            return found.group("crit").strip()
    return None


def attestation_now_acceptable(
    gerrit: GerritClient,
    change_id: str,
    revision: str,
    message: str | None,
    *,
    repo_root: Any = None,
) -> tuple[bool, str]:
    """Whether a ``finding`` block is ONLY the attestation criterion AND the ticket is now attested.

    Both inputs are PRIVILEGED-SIDE, preserving this module's core invariant: the bot's own durable
    verdict message, and the ticket store. Nothing the requesting comment says is consulted — the
    criterion id comes from the bot's message and the ticket id from the change's own commit-message
    trailer, never from the comment or the finding's (clipped) detail text.

    Reuses the gate's OWN classifier and bucketing (``classify_plan_review_attestation`` /
    ``bucket_for_verdict``) rather than a second notion of "acceptable attestation", which would
    drift from the gate that produced the finding.

    Returns ``(eligible, reason)``; every failure path is ``False`` (fail-closed).
    """
    criterion = sole_blocking_criterion(message)
    if criterion != ATTESTATION_CRITERION:
        return False, f"sole-blocking-criterion={criterion!r}"
    try:
        from rebar.llm.code_review.bugfix_size_gate import (
            bucket_for_verdict,
            classify_plan_review_attestation,
            ticket_for_commit_message,
        )

        commit = gerrit.get_commit(change_id, revision) or {}
        ticket_id = ticket_for_commit_message(str(commit.get("message") or ""), repo_root=repo_root)
        if not ticket_id:
            return False, "no-ticket-in-commit-trailer"
        classification = classify_plan_review_attestation(ticket_id, repo_root=repo_root)
        verdict = str((classification or {}).get("verdict") or "")
        bucket = bucket_for_verdict(verdict)
        if bucket != "accepted":
            return False, f"attestation-{bucket}:{verdict}"
        return True, f"attestation-accepted:{verdict}"
    except Exception as exc:
        logger.warning("retrigger: attestation re-check failed for %s", change_id, exc_info=True)
        return False, f"attestation-check-error:{exc!r}"


def latest_bot_tag_state(
    gerrit: GerritClient, change_id: str, current_patchset: int | None
) -> str | None:
    """The classification of the bot's LATEST vote message on the CURRENT revision.

    Reads ``GET /a/changes/{id}/messages`` (ChangeMessageInfo, oldest first), keeps only
    messages for ``current_patchset`` (``_revision_number``) that carry the bot's
    ``autogenerated:rebar`` tag (set by ``post_vote`` — the durable authorship marker,
    robust where ``AccountInfo`` detail is not), and returns the newest one's
    classification. ``None`` means the current revision has no bot verdict message —
    a vote-less change, which is ELIGIBLE (equivalent to a reconciler re-drive)."""
    state: str | None = None
    for msg in gerrit.get_change_messages(change_id):
        if not isinstance(msg, dict):
            continue
        if current_patchset is not None and msg.get("_revision_number") != current_patchset:
            continue
        if not str(msg.get("tag") or "").startswith("autogenerated:rebar"):
            continue
        classified = classify_tag(msg.get("message"))
        if classified is not None:
            state = classified
    return state


def _refusal_message(state: str, *, detail: str | None = None) -> str:
    base = (
        "rerun-llm-review refused: this revision's LLM-Review -1 is a REAL FINDING "
        f"(current tag state: {state}), not an infrastructure coverage gap. "
        "Address the finding and push a new patchset (git commit --amend + re-push); "
        "the new revision is reviewed automatically. rerun-llm-review only re-runs "
        "reviews that failed for infrastructure reasons."
    )
    # Name the attestation verdict when that was the reason (ticket 6a81), so a contributor whose
    # attestation is stale or unsigned is told WHICH state blocked rather than being sent to amend
    # code they may not need to touch.
    if detail and detail.startswith("attestation-"):
        base += f" (attestation re-check: {detail})"
    return base


_ACCEPTED_MESSAGE = (
    "rerun-llm-review accepted: a fresh fail-closed LLM review of the current revision "
    "has been queued and its automatic-retry budget re-armed. The verdict will be "
    "posted as a new vote when the review completes."
)


#: Reply to the RETIRED trigger word. It must NOT spell that word: this text is posted as a
#: Gerrit comment, and the retired word embeds CI's substring-matched ChatOps keyword — a
#: reply quoting it would re-dispatch the Verified workflow from the bot's own comment.
_RETIRED_TOKEN_MESSAGE = (
    "The LLM re-review trigger word has changed: comment `rerun-llm-review` instead. "
    "The former word was retired because it embedded CI's re-run keyword, so posting it "
    "also re-dispatched the Verified workflow and cancelled in-flight CI runs. No review "
    "was queued for this comment — post `rerun-llm-review` to request one."
)


def _actionable_change_key(cfg: ReceiverConfig, event: dict) -> str | None:
    """The change's key, or ``None`` when the event is not ours to act on.

    Folds the project guard and the key lookup together: an event for another project, or one
    with no usable change key, yields ``None`` and every caller path silently no-ops."""
    change = event.get("change") or {}
    project = change.get("project")
    if cfg.project and project and project != cfg.project:
        return None
    key = change.get("id") or change.get("number")
    return str(key) if key else None


def _trigger_mode(comment: str) -> str | None:
    """``"current"`` for the live trigger word, ``"retired"`` for the retired one, else ``None``.

    A comment carrying BOTH resolves to ``"current"``: quoting the retired word while also
    asking properly must not downgrade the request into a refusal."""
    if _TOKEN_RE.search(comment) is not None:
        return "current"
    if _RETIRED_TOKEN_RE.search(comment) is not None:
        return "retired"
    return None


def _refuse_retired_word(gc: GerritClient, change_id: str, revision: str, author: str) -> None:
    """Reply to the RETIRED trigger word (hard cutover, ticket 0d78-6c15-db26-4cea).

    No eligibility read, no label write, exactly one best-effort reply naming the live
    trigger. Callers invoke this only AFTER the project guard and the change re-anchoring,
    because ``post_comment`` needs the re-anchored ``change_id``/``revision`` and the bot
    must never reply on an out-of-project change."""
    _emit(
        logging.INFO,
        "RETRIGGER_REFUSED",
        change_id=change_id,
        revision_id=revision,
        reason="retired-trigger-word",
        detail=f"use {TRIGGER_TOKEN}",
        requested_by=author,
    )
    try:
        gc.post_comment(change_id, revision, _RETIRED_TOKEN_MESSAGE)
    except GerritError:
        logger.warning("retrigger: retired-word reply failed for %s (non-fatal)", change_id)


def _durable_reset_failure(
    gerrit: GerritClient, change_id: str, revision: str
) -> tuple[str, str] | None:
    """Reset to neutral and verify all accounts; return refusal reason/detail on failure."""
    try:
        gerrit.reset_llm_review_vote(change_id, revision)
    except GerritError as exc:
        return "vote-reset-error", str(exc)
    try:
        has_vote = gerrit.has_llm_review_vote(change_id, revision)
    except GerritError as exc:
        return "vote-verification-error", str(exc)
    if has_vote:
        return "nonzero-vote", "a nonzero LLM-Review vote remains after reset"
    return None


def handle_comment_added(
    event: dict,
    *,
    config: ReceiverConfig | None = None,
    gerrit: GerritClient | None = None,
    dedup: DedupStore | None = None,
) -> dict[str, Any] | None:
    """Process one Gerrit ``comment-added`` event; return the forced review event to
    enqueue on acceptance, else ``None``.

    Order of checks (each earlier check sees strictly less attacker-controlled input):
    loop guard (bot's own comments) → token word-match (current OR retired) → project
    guard → change re-anchoring → retired-word refusal → eligibility from the bot's own
    durable vote-message tag → fixed-neutral reset → all-accounts verification → local
    budget reset. Replies are best-effort — a failed reply must
    not lose the accepted re-review or crash the worker."""
    if not isinstance(event, dict):
        return None
    cfg = config or ReceiverConfig.from_env()

    author = str(((event.get("author") or {}).get("username")) or "")
    if author == cfg.bot_user:
        return None  # loop guard: our own replies re-arrive as comment-added events

    mode = _trigger_mode(str(event.get("comment") or ""))
    if mode is None:
        return None  # not addressed to us — silent skip (every human comment lands here)

    change_key = _actionable_change_key(cfg, event)
    if change_key is None:
        return None

    gc = gerrit or GerritClient(cfg)

    # Re-anchor on the CURRENT revision (the comment may sit on an older patchset view;
    # the /rerun path re-anchors identically via the same helper).
    try:
        fresh = gc.get_change_event(change_key)
    except GerritError as exc:
        _emit(
            logging.WARNING,
            "RETRIGGER_REFUSED",
            change_id=change_key,
            reason="eligibility-read-error",
            error=str(exc),
            requested_by=author,
        )
        return None
    if fresh is None:
        _emit(
            logging.WARNING,
            "RETRIGGER_REFUSED",
            change_id=change_key,
            reason="change-not-found",
            requested_by=author,
        )
        return None

    change_id = str(fresh["change"]["id"])
    revision = str(fresh["patchSet"]["revision"])
    patchset_number = fresh["patchSet"].get("number")

    if mode == "retired":
        _refuse_retired_word(gc, change_id, revision, author)
        return None

    try:
        state = latest_bot_tag_state(gc, change_id, patchset_number)
    except GerritError as exc:
        _emit(
            logging.WARNING,
            "RETRIGGER_REFUSED",
            change_id=change_id,
            revision_id=revision,
            reason="eligibility-read-error",
            error=str(exc),
            requested_by=author,
        )
        return None
    if state == "finding":
        # ONE exception to the findings-block refusal (ticket 6a81): a block that is ONLY the
        # bugfix-size-attestation criterion, whose remedy is a ticket action rather than a code
        # change, once the named ticket actually carries an acceptable plan-review attestation.
        # Refusing that case forces a cosmetic code edit purely to re-trigger the review, because a
        # no-code-change patchset copies the -1 forward.
        eligible, why = attestation_now_acceptable(
            gc, change_id, revision, latest_bot_message(gc, change_id, patchset_number)
        )
        if not eligible:
            _emit(
                logging.WARNING,
                "RETRIGGER_REFUSED",
                change_id=change_id,
                revision_id=revision,
                reason="finding-block",
                detail=why,
                requested_by=author,
            )
            try:
                gc.post_comment(change_id, revision, _refusal_message(state, detail=why))
            except GerritError:
                logger.warning("retrigger: refusal reply failed for %s (non-fatal)", change_id)
            return None
        _emit(
            logging.INFO,
            "RETRIGGER_ATTESTATION_UNBLOCKED",
            change_id=change_id,
            revision_id=revision,
            reason=why,
            requested_by=author,
        )

    # Eligible: create and verify the durable Gerrit marker before touching local state.
    reset_failure = _durable_reset_failure(gc, change_id, revision)
    if reset_failure is not None:
        reason, error = reset_failure
        _emit(
            logging.WARNING,
            "RETRIGGER_REFUSED",
            change_id=change_id,
            revision_id=revision,
            reason=reason,
            error=error,
            requested_by=author,
        )
        return None

    # Durable neutral state is proven: best-effort re-arm the 0347 automatic-retry
    # budget, then hand back a forced live event for the fast path.
    store = dedup or DedupStore(cfg.dedup_db_path)
    try:
        store.reset_attempts(change_id, revision)
    except Exception as exc:  # noqa: BLE001 — durable Gerrit marker already established
        _emit(
            logging.WARNING,
            "RETRIGGER_ATTEMPT_RESET_ERROR",
            change_id=change_id,
            revision_id=revision,
            error=str(exc),
            requested_by=author,
        )

    fresh["_rebar_force"] = True
    _emit(
        logging.INFO,
        "RETRIGGER_ACCEPTED",
        change_id=change_id,
        revision_id=revision,
        tag_state=state,
        requested_by=author,
    )
    try:
        gc.post_comment(change_id, revision, _ACCEPTED_MESSAGE)
    except GerritError:
        logger.warning("retrigger: queued reply failed for %s (non-fatal)", change_id)
    return fresh
