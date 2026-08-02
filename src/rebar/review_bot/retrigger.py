"""Contributor-triggerable re-review — the ``recheck-review`` comment trigger (ticket bb9b).

WHAT THIS IS. The review-bot's fail-closed design leaves two states a CONTRIBUTOR could
previously only escape by finding an operator: a coverage-gap ``-1`` whose underlying
infra failure has since been corrected, and a retries-exhausted escalation (ticket 0347)
whose bounded automatic recovery has been spent. This module lets the contributor comment
``recheck-review`` on the Gerrit change to request a FRESH review through the same
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
* Nothing in this path writes a vote. Replies go through
  :meth:`GerritClient.post_comment`, whose request body has NO ``labels`` key at all.
* The bot's own comments never trigger (loop guard), so the reply cannot re-trigger.

On acceptance the 0347 retry budget is re-armed (``reset_attempts`` — the row DELETE)
before the forced re-review is enqueued, so a change that already exhausted its automatic
retries gets a fresh bounded budget once the human says the underlying failure is fixed.
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

#: The magic comment token. Distinct from CI's ``recheck`` (which re-runs the Verified
#: gate) — ``recheck-review`` re-runs the LLM-Review gate.
TRIGGER_TOKEN = "recheck-review"

#: Word-boundary match where ``-`` and ``_`` also bind: ``recheck-review`` triggers,
#: ``recheck-reviews`` / ``pre-recheck-review`` / ``recheck-review-x`` do not.
_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_-])recheck-review(?![A-Za-z0-9_-])")

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


def _refusal_message(state: str) -> str:
    return (
        "recheck-review refused: this revision's LLM-Review -1 is a REAL FINDING "
        f"(current tag state: {state}), not an infrastructure coverage gap. "
        "Address the finding and push a new patchset (git commit --amend + re-push); "
        "the new revision is reviewed automatically. recheck-review only re-runs "
        "reviews that failed for infrastructure reasons."
    )


_ACCEPTED_MESSAGE = (
    "recheck-review accepted: a fresh fail-closed LLM review of the current revision "
    "has been queued and its automatic-retry budget re-armed. The verdict will be "
    "posted as a new vote when the review completes."
)


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
    loop guard (bot's own comments) → token word-match → project guard → eligibility
    from the bot's own durable vote-message tag. Replies are best-effort — a failed
    reply must not lose the accepted re-review or crash the worker."""
    if not isinstance(event, dict):
        return None
    cfg = config or ReceiverConfig.from_env()

    author = str(((event.get("author") or {}).get("username")) or "")
    if author == cfg.bot_user:
        return None  # loop guard: our own replies re-arrive as comment-added events

    if _TOKEN_RE.search(str(event.get("comment") or "")) is None:
        return None  # not addressed to us — silent skip (every human comment lands here)

    change = event.get("change") or {}
    project = change.get("project")
    if cfg.project and project and project != cfg.project:
        return None
    change_key = change.get("id") or change.get("number")
    if not change_key:
        return None

    gc = gerrit or GerritClient(cfg)

    # Re-anchor on the CURRENT revision (the comment may sit on an older patchset view;
    # the /rerun path re-anchors identically via the same helper).
    fresh = gc.get_change_event(str(change_key))
    if fresh is None:
        _emit(
            logging.WARNING,
            "RETRIGGER_REFUSED",
            change_id=str(change_key),
            reason="change-not-found",
            requested_by=author,
        )
        return None

    change_id = str(fresh["change"]["id"])
    revision = str(fresh["patchSet"]["revision"])
    patchset_number = fresh["patchSet"].get("number")

    state = latest_bot_tag_state(gc, change_id, patchset_number)
    if state == "finding":
        _emit(
            logging.WARNING,
            "RETRIGGER_REFUSED",
            change_id=change_id,
            revision_id=revision,
            reason="finding-block",
            requested_by=author,
        )
        try:
            gc.post_comment(change_id, revision, _refusal_message(state))
        except GerritError:
            logger.warning("retrigger: refusal reply failed for %s (non-fatal)", change_id)
        return None

    # Eligible: re-arm the 0347 automatic-retry budget, then hand back a forced event.
    store = dedup or DedupStore(cfg.dedup_db_path)
    try:
        store.reset_attempts(change_id, revision)
    except Exception:  # noqa: BLE001 — a budget-reset failure must not block the re-review
        logger.exception("retrigger: reset_attempts failed for %s (continuing)", change_id)

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
