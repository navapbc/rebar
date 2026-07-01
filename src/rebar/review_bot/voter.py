"""The voter — review a patchset and cast the ``LLM-Review`` vote (epic d251 / S4b).

This is the receiver's critical section. Given a Gerrit ``patchset-created`` webhook
(or a reconciler-synthesized event), it:

1. extracts the change/revision/ref/project and skips non-rebar projects;
2. takes a per-``(change_id, revision)`` single-flight lock (a webhook + its retries +
   the backfill reconciler all target the same key, so only one review runs at a time);
3. short-circuits if the vote is already recorded locally (dedup store) OR already
   present on Gerrit (the authoritative check) — a webhook + backfill never double-vote;
4. clones the change ref into a temp working tree, fetches the diff, and runs the
   ``adapter.code_review_decision`` seam;
5. maps PASS→``LLM_REVIEW_MAX_VALUE`` / BLOCK→``LLM_REVIEW_BLOCK_VALUE`` and casts the
   vote via Gerrit REST;
6. records the dedup row ONLY on a confirmed-successful vote (write-on-success). ANY
   failure (exception, non-2xx, adapter BLOCK-on-error) logs a structured ``VOTER_ERROR``
   JSON line and leaves the change unsubmittable — a MAX is NEVER cast on failure.

Fail-closed throughout: a missed/failed review only DELAYS submittability (the change
stays unsubmittable); it can never let an unreviewed change merge.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import time
from typing import Any

from rebar.review_bot import adapter
from rebar.review_bot.config import ReceiverConfig
from rebar.review_bot.dedup import DedupStore
from rebar.review_bot.gerrit_client import GerritClient, GerritError

logger = logging.getLogger("rebar.review_bot.voter")

# Module-level per-(change_id, revision) single-flight locks. A webhook, its
# at-least-once retries, and the backfill reconciler all key on the same pair, so
# routing them through one asyncio.Lock serializes the review (the dedup/Gerrit check
# inside the lock then makes the later ones a no-op skip).
# NOTE (PoC scope): this dict grows by one small entry per (change, revision) over
# the process lifetime — an accepted, bounded leak on the single-box PoC (the box is
# rebuilt from IaC, and the entry count tracks distinct patchsets reviewed). A
# longer-lived deployment would add an LRU cap / post-release eviction.
_locks: dict[tuple[str, str], asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def _lock_for(key: tuple[str, str]) -> asyncio.Lock:
    async with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _locks[key] = lock
        return lock


def _emit(level: int, event: str, **fields: Any) -> None:
    """Emit one structured JSON log line. The ``VOTER_ERROR`` event is the marker the
    host observability probe greps for to publish ``rebar/host:voter_errors``."""
    record = {"event": event, "timestamp": time.time(), **fields}
    logger.log(level, json.dumps(record, default=str))


def _voter_error(**fields: Any) -> None:
    """Structured fail-closed marker (greppable: ``VOTER_ERROR``). Always to stderr too
    so it lands in journald even if logging is misconfigured."""
    record = {
        "event": "VOTER_ERROR",
        "timestamp": time.time(),
        "change_id": fields.get("change_id"),
        "revision_id": fields.get("revision_id"),
        "vote_value": fields.get("vote_value"),
        "http_status": fields.get("http_status"),
        "error": fields.get("error"),
    }
    line = "VOTER_ERROR " + json.dumps(record, default=str)
    logger.error(line)
    # Also write straight to stderr (journald) so the greppable VOTER_ERROR marker — the
    # source for the rebar/host:voter_errors metric — lands even if logging is reconfigured.
    print(line, file=sys.stderr, flush=True)  # noqa: T201 — intentional journald marker
    _publish_voter_error_metric()


def _publish_voter_error_metric() -> None:
    """Best-effort direct publish of ``rebar/host:voter_errors`` via boto3 (instance
    role). The journald → host-probe path (infra/.../observability.sh) is the RELIABLE
    fallback — in-container boto3 may not reach IMDS for credentials (the container's
    IMDS hop limit can preclude it), so any ImportError / boto / credential / network
    failure is silently swallowed and we rely on the journald marker above."""
    try:
        import boto3  # noqa: PLC0415 — optional, lazy: only on a fail-closed error path

        boto3.client("cloudwatch").put_metric_data(
            Namespace="rebar/host",
            MetricData=[{"MetricName": "voter_errors", "Value": 1, "Unit": "Count"}],
        )
    except Exception:  # noqa: BLE001 — IMDS hop limit / no creds / offline: journald is the fallback
        pass


def _extract(event: dict) -> dict[str, Any] | None:
    """Pull the fields the voter needs out of a Gerrit ``patchset-created`` payload.

    Gerrit shape: ``change.id``/``change.number``/``change.project`` and
    ``patchSet.number``/``patchSet.revision``/``patchSet.ref``. Returns ``None`` if the
    payload is missing the essentials (a malformed event is skipped, not crashed on)."""
    if not isinstance(event, dict):
        return None
    change = event.get("change") or {}
    patchset = event.get("patchSet") or event.get("patchset") or {}
    change_id = change.get("id")
    revision = patchset.get("revision")
    ref = patchset.get("ref")
    if not change_id or not revision or not ref:
        return None
    return {
        "change_id": str(change_id),
        "change_number": change.get("number"),
        "project": change.get("project"),
        "revision": str(revision),
        "patchset_ref": str(ref),
        "patchset_number": patchset.get("number"),
        "event_type": event.get("type") or "patchset-created",
    }


async def review_and_vote(
    event: dict,
    *,
    config: ReceiverConfig | None = None,
    gerrit: GerritClient | None = None,
    dedup: DedupStore | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Review the patchset described by ``event`` and cast the ``LLM-Review`` vote.

    Returns a small status dict (``{status, change_id, revision, vote_value?}``) for
    observability/tests. ``status`` is one of ``skipped`` (non-rebar / malformed /
    already voted), ``voted`` (a vote was cast), or ``error`` (fail-closed: logged
    VOTER_ERROR, no vote / a BLOCK vote, never MAX-on-failure)."""
    cfg = config or ReceiverConfig.from_env()
    info = _extract(event)
    if info is None:
        _emit(logging.INFO, "voter_skip", reason="malformed_event")
        return {"status": "skipped", "reason": "malformed_event"}

    if cfg.project and info["project"] and info["project"] != cfg.project:
        _emit(
            logging.INFO,
            "voter_skip",
            reason="other_project",
            change_id=info["change_id"],
            project=info["project"],
        )
        return {"status": "skipped", "reason": "other_project", "change_id": info["change_id"]}

    change_id = info["change_id"]
    revision = info["revision"]
    key = (change_id, revision)
    gc = gerrit or GerritClient(cfg)
    store = dedup or DedupStore(cfg.dedup_db_path)

    lock = await _lock_for(key)
    async with lock:
        # Dedup + existing-vote short-circuits are SKIPPED when force=True (a manual
        # /rerun): forcing re-reviews even a change that already carries a vote (e.g.
        # a stuck fail-closed -1), overwriting it with a fresh verdict. force still
        # runs the full review + is still fail-closed — it can only request a fresh
        # review, never force a PASS.
        # Dedup short-circuit (local ledger first — cheap, no network).
        if not force and store.already_voted(change_id, revision):
            _emit(
                logging.INFO,
                "voter_skip",
                reason="dedup",
                change_id=change_id,
                revision_id=revision,
            )
            return {"status": "skipped", "reason": "dedup", "change_id": change_id}
        # Authoritative Gerrit-side guard (catches a lost dedup row / fresh box / an
        # admin vote). A failure HERE is fail-closed: we do not proceed to cast blindly.
        try:
            if not force and await asyncio.to_thread(gc.has_llm_review_vote, change_id, revision):
                _emit(
                    logging.INFO,
                    "voter_skip",
                    reason="already_voted_gerrit",
                    change_id=change_id,
                    revision_id=revision,
                )
                return {
                    "status": "skipped",
                    "reason": "already_voted_gerrit",
                    "change_id": change_id,
                }
        except GerritError as exc:
            _voter_error(
                change_id=change_id,
                revision_id=revision,
                vote_value=None,
                http_status=getattr(exc, "status", None),
                error=f"has_llm_review_vote: {exc}",
            )
            return {"status": "error", "change_id": change_id, "stage": "dedup_check"}

        # Review: clone the ref, fetch the diff, run the adapter seam.
        try:
            with tempfile.TemporaryDirectory(prefix="reviewbot-") as repo_root:
                await asyncio.to_thread(
                    gc.clone_change_ref, info["change_number"], info["patchset_ref"], repo_root
                )
                diff_text = await asyncio.to_thread(gc.get_patch, change_id, revision)
                decision = await asyncio.to_thread(
                    adapter.code_review_decision,
                    diff_text,
                    repo_root,
                    info["patchset_ref"],
                    config=cfg,
                )
        except GerritError as exc:
            # A clone/diff failure → cannot review → fail-closed BLOCK vote attempt below
            # would itself need a usable Gerrit; surface the error and leave unsubmittable.
            _voter_error(
                change_id=change_id,
                revision_id=revision,
                vote_value=None,
                http_status=getattr(exc, "status", None),
                error=f"review_setup: {exc}",
            )
            return {"status": "error", "change_id": change_id, "stage": "review_setup"}

        # Map decision → vote value. BLOCK (incl. adapter fail-closed) → block value;
        # PASS → max value. A MAX is cast ONLY on an explicit PASS.
        is_pass = decision.get("decision") == "PASS"
        value = cfg.llm_review_max_value if is_pass else cfg.llm_review_block_value
        message = decision.get("message") or "rebar code review."

        try:
            http_status = await asyncio.to_thread(gc.post_vote, change_id, revision, value, message)
        except GerritError as exc:
            # Vote POST failed → DO NOT record dedup (so a retry re-attempts) and never
            # leave a half-cast MAX: the change simply stays unsubmittable.
            _voter_error(
                change_id=change_id,
                revision_id=revision,
                vote_value=value,
                http_status=getattr(exc, "status", None),
                error=f"post_vote: {exc}",
            )
            return {"status": "error", "change_id": change_id, "stage": "post_vote"}

        # Write-on-success: only now is the (change, revision) recorded as voted.
        store.record_vote(change_id, revision, info["event_type"], value)
        _emit(
            logging.INFO,
            "voter_voted",
            change_id=change_id,
            revision_id=revision,
            vote_value=value,
            http_status=http_status,
            decision=decision.get("decision"),
        )
        return {
            "status": "voted",
            "change_id": change_id,
            "revision": revision,
            "vote_value": value,
            "decision": decision.get("decision"),
        }
