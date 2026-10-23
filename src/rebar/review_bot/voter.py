"""Review a Gerrit revision and cast its LLM-Review vote.
Webhook and backfill share a per-change-revision lock. Check local dedup
and Gerrit votes, clone the ref, bind the reviewed tree, run the gate,
and post the decision. Record dedup only after a successful vote.
Retryable coverage gaps may defer without voting. A proven tree
mismatch refuses any vote, and failures never mint an unearned PASS."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import tempfile
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from rebar.review_bot import adapter
from rebar.review_bot import startup as _startup
from rebar.review_bot.artifact_emit import emit_code_review_artifact
from rebar.review_bot.config import ReceiverConfig
from rebar.review_bot.dedup import DedupStore
from rebar.review_bot.finding_publish import post_review
from rebar.review_bot.gerrit_client import GerritClient, GerritError
from rebar.review_bot.low_disk import GAP_REASON as _LOW_DISK_GAP_REASON
from rebar.review_bot.low_disk import exhausted_result as _low_disk_exhausted_result
from rebar.review_bot.low_disk import pre_clone_refusal as _pre_clone_refusal
from rebar.review_bot.voter_merge import (
    assemble_merge_diff as _assemble_merge_diff,
)
from rebar.review_bot.voter_merge import (
    merge_change_error as _merge_change_error,
)
from rebar.review_bot.voter_merge import (
    merge_coverage_gap_decision as _merge_coverage_gap_decision,
)
from rebar.review_bot.voter_merge import (
    render_diff_info as _render_diff_info,  # noqa: F401 — re-exported for tests
)

if TYPE_CHECKING:
    from rebar.llm.auth import LLMRuntime

logger = logging.getLogger("rebar.review_bot.voter")

# Serialize review by change and revision so webhook and backfill
# cannot vote concurrently. Local and Gerrit checks inside the lock
# skip a later duplicate. Lock entries persist for this process.
_locks: dict[tuple[str, str], asyncio.Lock] = {}
_locks_guard = asyncio.Lock()

# Count the whole voter call, including cheap skips, for health and
# deploy deferral. Counter updates stay on the event-loop thread while
# blocking work is offloaded.
_in_flight = 0


def in_flight_reviews() -> int:
    """Return reviews currently executing through webhook or backfill.
    The health and deploy paths use this count to avoid interrupting work."""
    return _in_flight


@contextlib.contextmanager
def _counting_in_flight() -> Iterator[None]:
    """Hold :data:`_in_flight` up for the duration of one review."""
    global _in_flight
    _in_flight += 1
    try:
        yield
    finally:
        _in_flight -= 1


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
    """Emit one greppable VOTER_ERROR line on stderr plus a structured
    log body, without double-counting the marker."""
    record = {
        "event": "VOTER_ERROR",
        "timestamp": time.time(),
        "change_id": fields.get("change_id"),
        "revision_id": fields.get("revision_id"),
        "vote_value": fields.get("vote_value"),
        "http_status": fields.get("http_status"),
        "error": fields.get("error"),
    }
    body = json.dumps(record, default=str)
    logger.error(body)
    # Write the token-prefixed marker straight to stderr (journald) so the greppable
    # VOTER_ERROR line — the source for the rebar/host:voter_errors metric — lands exactly
    # once, whether or not logging is configured.
    print("VOTER_ERROR " + body, file=sys.stderr, flush=True)  # noqa: T201 — intentional journald marker
    _publish_voter_error_metric()


def _publish_voter_error_metric() -> None:
    """Publish voter_errors to CloudWatch best-effort. The journald marker
    remains available when container credentials or network access fail."""
    try:
        import boto3

        boto3.client("cloudwatch").put_metric_data(
            Namespace="rebar/host",
            MetricData=[{"MetricName": "voter_errors", "Value": 1, "Unit": "Count"}],
        )
    except Exception:  # noqa: BLE001 — IMDS hop limit / no creds / offline: journald is the fallback
        pass


# Use the runner's token fields from coverage metrics for usage
# observability.
_TOKEN_METRIC_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "total_tokens",
)


def _publish_token_usage_metrics(metrics: dict[str, Any]) -> None:
    """Publish available review token totals to CloudWatch best-effort.
    Missing fields contribute zero. No data skips publication. The
    journald usage marker remains the fallback."""
    data = [
        {
            "MetricName": f"review_bot_llm_{field}",
            "Value": float(int(metrics.get(field) or 0)),
            "Unit": "Count",
        }
        for field in _TOKEN_METRIC_FIELDS
    ]
    if all(entry["Value"] == 0 for entry in data):
        return  # no token data recorded for this review — nothing to publish
    try:
        import boto3

        boto3.client("cloudwatch").put_metric_data(Namespace="rebar/host", MetricData=data)
    except Exception:  # noqa: BLE001 — IMDS hop limit / no creds / offline: journald is the fallback
        pass


def _emit_token_usage(change_id: str, revision: str, metrics: dict[str, Any]) -> None:
    """Emit one LLM_TOKEN_USAGE stderr marker and try CloudWatch.
    Accounting failures cannot change an already cast vote."""
    try:
        record = {
            "event": "LLM_TOKEN_USAGE",
            "timestamp": time.time(),
            "change_id": change_id,
            "revision_id": revision,
            **{f: int(metrics.get(f) or 0) for f in _TOKEN_METRIC_FIELDS},
        }
        body = json.dumps(record, default=str)
        logger.info(body)
        print("LLM_TOKEN_USAGE " + body, file=sys.stderr, flush=True)  # noqa: T201 — intentional journald marker
        _publish_token_usage_metrics(metrics)
    except Exception:
        logger.warning("token-usage emission failed; continuing", exc_info=True)


def _extract(event: dict) -> dict[str, Any] | None:
    """Extract change, project, revision, and ref from a patchset event.
    Return None when required fields are missing."""
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
    runtime: LLMRuntime | None = None,
) -> dict[str, Any]:
    """Run the shared webhook and backfill voter under in-flight accounting.
    Return a status record for skipped, deferred, voted, or failed work.
    Failures remain fail-closed and never cast a maximum vote."""
    with _counting_in_flight():
        return await _review_and_vote(
            event, config=config, gerrit=gerrit, dedup=dedup, force=force, runtime=runtime
        )


async def _decision_for_clone(
    gc: Any,
    repo_root: str,
    info: dict[str, Any],
    *,
    is_merge: bool,
    parent_count: int,
    commit_message: str,
    runtime: LLMRuntime | None = None,
) -> tuple[dict[str, Any], str]:
    """Fetch the cloned revision's merge or ordinary diff and run the adapter.
    Pass revision for tree-to-vote binding. Return decision and reviewed
    diff for later artifact fingerprinting."""
    change_id, revision = info["change_id"], info["revision"]
    if not is_merge:
        diff_text = await asyncio.to_thread(gc.get_patch, change_id, revision)
        decision = await asyncio.to_thread(
            adapter.code_review_decision,
            diff_text,
            repo_root,
            info["patchset_ref"],
            revision=revision,  # binds the reviewed tree to the voted revision
            commit_message=commit_message,  # scope-intent overlay (non-merge path)
            change_id=change_id,  # change:<id> novelty keyspace (finding-memory)
            runtime=runtime,  # forwarded composed startup runtime (S5)
        )
        return decision, diff_text
    # Route merge revisions around bare /patch, which returns 409.
    # Log the guard when it selects the auto-merge delta.
    _emit(
        logging.INFO,
        "merge_change_409_guard",
        change_id=change_id,
        revision_id=revision,
        parent_count=parent_count,
    )
    # ONLY the auto-merge delta + integrated-commit context — never /patch. A merge-path REST
    # failure here is a fail-closed -1 coverage-gap (the clone succeeded, so the vote POST
    # further up can still reach Gerrit).
    try:
        diff_text, merge_commits, stats = await asyncio.to_thread(
            _assemble_merge_diff, gc, change_id, revision
        )
    except GerritError as exc:
        return _merge_coverage_gap_decision(f"merge context assembly failed: {exc}"), ""
    # Log WHAT the reviewer saw (context stats) so a merge review can be debugged from logs
    # alone: empty auto-merge delta, a truncated REST fan-out, or an unexpected file/commit
    # count are all visible here.
    _emit(
        logging.INFO,
        "merge_change_review",
        change_id=change_id,
        revision_id=revision,
        integrated_commits=merge_commits,
        **stats,
    )
    decision = await asyncio.to_thread(
        adapter.code_review_decision,
        diff_text,
        repo_root,
        info["patchset_ref"],
        revision=revision,  # binds the reviewed tree to the voted revision
        merge_commits=merge_commits,
        change_id=change_id,  # change:<id> novelty keyspace
        runtime=runtime,  # forwarded composed startup runtime (S5)
    )
    return decision, diff_text


async def _decision_for_review_target(
    gc: Any,
    info: dict[str, Any],
    *,
    cfg: ReceiverConfig,
    is_merge: bool,
    parent_count: int,
    commit_message: str,
    runtime: LLMRuntime | None = None,
) -> tuple[dict[str, Any], str]:
    """Run the review setup after the pre-clone disk admission check (see
    ``low_disk.pre_clone_refusal``), which must precede the ``TemporaryDirectory``."""
    if (refusal := _pre_clone_refusal(cfg)) is not None:
        return refusal, ""
    with tempfile.TemporaryDirectory(prefix="reviewbot-") as repo_root:
        await asyncio.to_thread(
            gc.clone_change_ref,
            info["change_number"],
            info["patchset_ref"],
            repo_root,
        )
        return await _decision_for_clone(
            gc,
            repo_root,
            info,
            is_merge=is_merge,
            parent_count=parent_count,
            commit_message=commit_message,
            runtime=runtime,
        )


def _guard_decision_auth(cfg: ReceiverConfig) -> dict[str, Any] | None:
    """Refuse before dedup, clone, or provider calls when Gerrit voting auth
    is blank. Emit VOTER_ERROR and never substitute another principal."""
    try:
        _startup.validate_decision_auth(cfg)
    except _startup.DecisionAuthError as exc:
        _voter_error(error=f"decision_auth: {exc}")
        return {"status": "error", "stage": "decision_auth"}
    return None


def _resolve_review_runtime(runtime: LLMRuntime | None, cfg: ReceiverConfig) -> LLMRuntime | None:
    """Use the supplied provider runtime or compose it from receiver config
    for the decision path."""
    if runtime is None:
        return _startup.compose_startup_binding(cfg).llm_runtime
    return runtime


def _handle_retryable_gap(
    decision: dict[str, Any],
    store: DedupStore,
    cfg: ReceiverConfig,
    change_id: str,
    revision: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Defer retryable coverage gaps without a vote while attempts remain.
    After exhaustion, non-disk gaps block with -1. Low disk remains a
    terminal no-vote deferral."""
    gap_reason = decision.get("gap_reason")
    if gap_reason not in adapter.RETRYABLE_GAP_REASONS:
        return decision, None
    try:
        attempts = store.record_attempt(change_id, revision)
    except Exception as exc:  # noqa: BLE001 — fail-open on the COUNTER, never the vote
        # An uncounted attempt only delays escalation by one reconcile cycle.
        _voter_error(
            change_id=change_id,
            revision_id=revision,
            vote_value=None,
            error=f"record_attempt: {exc}",
        )
        attempts = 0
    if attempts < cfg.retryable_gap_max_attempts:
        _emit(
            logging.WARNING,
            "REVIEW_RETRY_DEFERRED",
            change_id=change_id,
            revision_id=revision,
            gap_reason=gap_reason,
            attempt=attempts,
            max_attempts=cfg.retryable_gap_max_attempts,
        )
        return decision, {
            "status": "deferred",
            "change_id": change_id,
            "revision": revision,
            "gap_reason": gap_reason,
            "attempt": attempts,
        }
    if gap_reason == _LOW_DISK_GAP_REASON:
        _emit(
            logging.ERROR,
            "REVIEW_LOW_DISK_DEFERRED_EXHAUSTED",
            change_id=change_id,
            revision_id=revision,
            attempt=attempts,
            max_attempts=cfg.retryable_gap_max_attempts,
        )
        return decision, _low_disk_exhausted_result(
            change_id=change_id,
            revision=revision,
            attempts=attempts,
            max_attempts=cfg.retryable_gap_max_attempts,
        )
    decision = adapter.append_retries_exhausted_note(decision, attempts)
    _voter_error(
        change_id=change_id,
        revision_id=revision,
        vote_value=cfg.llm_review_block_value,
        error=(
            f"retryable coverage gap '{gap_reason}' exhausted its "
            f"{cfg.retryable_gap_max_attempts}-attempt budget — escalating to the "
            "fail-closed -1"
        ),
    )
    return decision, None


def _emit_review_token_usage(decision: dict[str, Any], change_id: str, revision: str) -> None:
    """Record the review's LLM token counts (from the verdict's ``coverage.metrics``) to
    journald + CloudWatch. A fail-closed review carries no verdict/metrics → nothing recorded.
    Best-effort: the vote is already cast, so this never fails the review."""
    metrics = ((decision.get("verdict") or {}).get("coverage") or {}).get("metrics") or {}
    if metrics:
        _emit_token_usage(change_id, revision, metrics)


async def _superseded_before_vote(
    gc: GerritClient,
    change_id: str,
    revision: str,
    *,
    force: bool,
    info: dict[str, Any],
) -> bool:
    """Suppress a vote when Gerrit reports a newer current revision
    before posting. Manual reruns and failed currency lookups proceed."""
    if force or info["event_type"] == "manual-rerun":
        return False
    try:
        current = await asyncio.to_thread(gc.get_change_event, change_id)
        current_rev = ((current or {}).get("patchSet") or {}).get("revision")
    except Exception:  # noqa: BLE001 — fail open: a Gerrit blip must not drop a review
        return False
    if not isinstance(current_rev, str) or not current_rev or current_rev == revision:
        return False
    _emit(
        logging.INFO,
        "voter_skip",
        reason="superseded",
        disposition="vote_suppressed_superseded",
        change_id=change_id,
        revision_id=revision,
        current_revision_id=current_rev,
        event_type=info["event_type"],
    )
    return True


async def _review_and_vote(
    event: dict,
    *,
    config: ReceiverConfig | None = None,
    gerrit: GerritClient | None = None,
    dedup: DedupStore | None = None,
    force: bool = False,
    runtime: LLMRuntime | None = None,
) -> dict[str, Any]:
    """Review a patchset; call review_and_vote so in-flight accounting is preserved."""
    cfg = config or ReceiverConfig.from_env()
    auth_error = _guard_decision_auth(cfg)
    if auth_error is not None:
        return auth_error
    runtime = _resolve_review_runtime(runtime, cfg)
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
        # Manual reruns skip existing-vote short-circuits but remain fail-closed.
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

        # Detect merges after the existing-vote guard and before diff fetch.
        # All entry paths share this decision. Merge REST failure becomes a
        # visible fail-closed coverage-gap vote.
        decision: dict[str, Any] | None = None
        parent_count = -1  # -1 = commit fetch failed (unknown); logged with the vote below
        commit_message = ""  # the change's commit body (drives scope-intent); "" if unknown
        diff_text = ""  # the reviewed diff (drives the code_review artifact's change_fingerprint)
        try:
            commit_info = await asyncio.to_thread(gc.get_commit, change_id, revision)
            parent_count = len(commit_info.get("parents") or [])
            commit_message = str(commit_info.get("message") or "")
            is_merge = parent_count >= 2
            # Log merge detection for every change so path selection is visible.
            _emit(
                logging.INFO,
                "merge_detection",
                change_id=change_id,
                revision_id=revision,
                parent_count=parent_count,
                is_merge=is_merge,
            )
        except GerritError as exc:
            _merge_change_error("merge_commit_error", "commit", change_id=change_id, error=str(exc))
            decision = _merge_coverage_gap_decision(f"commit fetch failed: {exc}")
            is_merge = False

        # Clone/materialize then run the adapter seam unless merge setup already failed.
        if decision is None:
            try:
                decision, diff_text = await _decision_for_review_target(
                    gc,
                    info,
                    cfg=cfg,
                    is_merge=is_merge,
                    parent_count=parent_count,
                    commit_message=commit_message,
                    runtime=runtime,
                )
            except adapter.ReviewedTreeMismatch as exc:
                # A proven checkout-revision mismatch permits no vote, including -1.
                # The reviewed tree is not the patchset receiving the label.
                _voter_error(
                    change_id=change_id,
                    revision_id=revision,
                    vote_value=None,
                    error=f"reviewed_tree_mismatch: {exc}",
                )
                return {
                    "status": "error",
                    "change_id": change_id,
                    "stage": "reviewed_tree_mismatch",
                }
            except GerritError as exc:
                # A clone / (non-merge) get_patch failure → cannot review → fail-closed. The
                # vote POST below would itself need a usable Gerrit; surface the error and
                # leave unsubmittable (no vote), matching the pre-S2 setup-failure behaviour.
                _voter_error(
                    change_id=change_id,
                    revision_id=revision,
                    vote_value=None,
                    http_status=getattr(exc, "status", None),
                    error=f"review_setup: {exc}",
                )
                return {"status": "error", "change_id": change_id, "stage": "review_setup"}

        decision, deferred = _handle_retryable_gap(decision, store, cfg, change_id, revision)
        if deferred is not None:
            return deferred

        # Map decision → vote value. BLOCK (incl. adapter fail-closed) → block value;
        # PASS → max value. A MAX is cast ONLY on an explicit PASS.
        is_pass = decision.get("decision") == "PASS"
        value = cfg.llm_review_max_value if is_pass else cfg.llm_review_block_value
        message = decision.get("message") or "rebar code review."

        # Recheck revision currency before voting because review may
        # outlast a newer patchset. Suppress proven stale votes, but proceed
        # on lookup failure or a current-revision manual rerun.
        if await _superseded_before_vote(gc, change_id, revision, force=force, info=info):
            return {"status": "skipped", "reason": "superseded", "change_id": change_id}

        # Post the vote and valid inline findings. On inline rejection,
        # retry message-only with a notice so finding text remains visible.
        try:
            http_status = await asyncio.to_thread(
                post_review,
                gc,
                change_id,
                revision,
                value,
                message,
                decision.get("findings") or [],
            )
        except GerritError as exc:
            # Treat Gerrit 409 for a closed change as terminal. Record the skip
            # without a VOTER_ERROR or retry.
            if getattr(exc, "status", None) == 409:
                store.record_vote(change_id, revision, info["event_type"], value)
                _emit(
                    logging.INFO,
                    "voter_skip_closed",
                    change_id=change_id,
                    revision_id=revision,
                    http_status=409,
                )
                return {"status": "skipped", "change_id": change_id, "stage": "post_vote_closed"}
            # Any other vote POST failure → DO NOT record dedup (so a retry re-attempts) and
            # never leave a half-cast MAX: the change simply stays unsubmittable.
            _voter_error(
                change_id=change_id,
                revision_id=revision,
                vote_value=value,
                http_status=getattr(exc, "status", None),
                error=f"post_vote: {exc}",
            )
            return {"status": "error", "change_id": change_id, "stage": "post_vote"}

        # Write-on-success: only now is the (change, revision) recorded as voted. The retry
        # budget is per-revision and moot once a vote exists — clear it (best-effort).
        store.record_vote(change_id, revision, info["event_type"], value)
        with contextlib.suppress(Exception):
            store.reset_attempts(change_id, revision)
        _emit(
            logging.INFO,
            "voter_voted",
            change_id=change_id,
            revision_id=revision,
            vote_value=value,
            http_status=http_status,
            decision=decision.get("decision"),
            # Record merge path and parent count with the vote. A failed commit
            # fetch uses -1 as parent count.
            merge=is_merge,
            parent_count=parent_count,
        )
        # Token-usage observability (ticket clayish-basaltine-bug): record the review's LLM
        # token counts (from the verdict's coverage.metrics) to journald + CloudWatch. A
        # fail-closed review carries no verdict/metrics → nothing recorded. Best-effort.
        _emit_review_token_usage(decision, change_id, revision)
        # After voting, record a best-effort code_review artifact in the
        # ambient ticket store and link trailer-cited tickets. Offload its
        # synchronous store write so shutdown and health remain responsive.
        await asyncio.to_thread(
            emit_code_review_artifact,
            decision,
            change_id=change_id,
            revision=revision,
            commit_message=commit_message,
            diff_text=diff_text,
            repo_root=None,
        )
        return {
            "status": "voted",
            "change_id": change_id,
            "revision": revision,
            "vote_value": value,
            "decision": decision.get("decision"),
        }
