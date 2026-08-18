"""The voter — review a patchset and cast the ``LLM-Review`` vote (epic d251 / S4b).

This is the receiver's critical section. Given a Gerrit ``patchset-created`` webhook
(or a reconciler-synthesized event), it:

1. extracts the change/revision/ref/project and skips non-rebar projects;
2. takes a per-``(change_id, revision)`` single-flight lock (a webhook + its retries +
   the backfill reconciler all target the same key, so only one review runs at a time);
3. short-circuits if the vote is already recorded locally (dedup store) OR already
   present on Gerrit (the authoritative check) — a webhook + backfill never double-vote;
4. clones the change ref into a temp working tree, fetches the diff, and runs the
   ``adapter.code_review_decision`` seam — which first BINDS that tree to the revision
   the vote will attach to, and refuses the vote outright if they disagree;
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

# ── in-flight review accounting (bug 34cd; ADR 0068) ─────────────────────────────
# How many reviews are executing IN THIS PROCESS right now. Exported over ``/health`` so
# the deploy loop (infra/scripts/autodeploy.sh) can DEFER a container recreation that
# would otherwise KILL a running review — a recreation mid-review is INVISIBLE to every
# health signal the box has (the process was asked to stop, so nothing fails and no
# VOTER_ERROR is emitted), which is how a landing burst can live-lock the LLM-Review gate
# with `restarts=0` and all alarms OK.
#
# Counted around the WHOLE of ``review_and_vote``, including its cheap dedup /
# already-voted short-circuits, rather than only the expensive clone+LLM region. That
# over-counts by the few hundred ms such a skip takes, which is deliberate: the two error
# directions are NOT symmetric — over-counting delays a deploy by one ~2-minute timer tick,
# under-counting kills a ~10-minute review. Bias toward the recoverable one.
#
# A plain int needs no lock: every mutation below happens on the asyncio event-loop thread
# (the increment/decrement bracket the coroutine's own body, and the blocking work inside
# is offloaded with ``asyncio.to_thread`` while the count stays held by this coroutine), and
# ``/health`` is served on that same loop, so a reader never observes a torn value.
_in_flight = 0


def in_flight_reviews() -> int:
    """Number of reviews executing in this process right now (0 when idle).

    Covers BOTH review paths — the webhook queue worker and the backfill reconciler —
    because both funnel through :func:`review_and_vote`. That coverage is the point: the
    reconciler's inline backfill review is the path that RETRIES a killed review, so a
    busy-signal blind to it would let the deploy loop keep killing the very work that is
    supposed to heal the gate.
    """
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
        import boto3

        boto3.client("cloudwatch").put_metric_data(
            Namespace="rebar/host",
            MetricData=[{"MetricName": "voter_errors", "Value": 1, "Unit": "Count"}],
        )
    except Exception:  # noqa: BLE001 — IMDS hop limit / no creds / offline: journald is the fallback
        pass


# ── token-usage observability (ticket clayish-basaltine-bug) ─────────────────
# Names of the token fields the runner records on each call's `_usage` and that the
# code-review metrics assembler sums into `coverage['metrics']` (see
# rebar.llm.code_review.finalize._attach_code_review_metrics).
_TOKEN_METRIC_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "total_tokens",
)


def _publish_token_usage_metrics(metrics: dict[str, Any]) -> None:
    """Best-effort publish of the review's per-review LLM token counts to CloudWatch
    (``rebar/host:review_bot_llm_<field>_tokens``, ``Unit=Count``), mirroring
    :func:`_publish_voter_error_metric`. ``metrics`` is the verdict's
    ``coverage['metrics']`` dict (agent-step token totals; the Pass-1 finder batch is a
    documented follow-up gap). Any missing field contributes 0; a review with no token data
    publishes nothing. In-container boto3 may not reach IMDS, so any failure is swallowed — the
    ``LLM_TOKEN_USAGE`` journald marker emitted by the caller is the reliable fallback."""
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
    """Record the review's LLM token usage: a greppable ``LLM_TOKEN_USAGE`` journald line
    (the reliable, host-probe-parseable path) plus a best-effort direct CloudWatch publish.
    Best-effort throughout — the vote is already cast, so token accounting NEVER fails a
    review (any error is swallowed)."""
    try:
        record = {
            "event": "LLM_TOKEN_USAGE",
            "timestamp": time.time(),
            "change_id": change_id,
            "revision_id": revision,
            **{f: int(metrics.get(f) or 0) for f in _TOKEN_METRIC_FIELDS},
        }
        line = "LLM_TOKEN_USAGE " + json.dumps(record, default=str)
        logger.info(line)
        print(line, file=sys.stderr, flush=True)  # noqa: T201 — intentional journald marker
        _publish_token_usage_metrics(metrics)
    except Exception:
        logger.warning("token-usage emission failed; continuing", exc_info=True)


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
    runtime: LLMRuntime | None = None,
) -> dict[str, Any]:
    """Review the patchset described by ``event`` and cast the ``LLM-Review`` vote.

    Returns a small status dict (``{status, change_id, revision, vote_value?}``) for
    observability/tests. ``status`` is one of ``skipped`` (non-rebar / malformed /
    already voted), ``voted`` (a vote was cast), or ``error`` (fail-closed: logged
    VOTER_ERROR, no vote / a BLOCK vote, never MAX-on-failure).

    This is the single funnel for BOTH review paths (the webhook queue worker and the
    backfill reconciler), so bracketing it with :func:`_counting_in_flight` is what makes
    ``/health``'s ``in_flight`` count — and therefore the deploy loop's deferral — see
    every review that a container recreation could kill (bug 34cd).
    """
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
    """Fetch the diff for the ALREADY-CLONED tree at ``repo_root`` and run the
    ``adapter.code_review_decision`` seam, taking the merge or non-merge diff path.

    ``revision`` is handed to the seam so the adapter can bind the tree it is about to review
    to the revision the vote will attach to; a proven disagreement raises
    ``adapter.ReviewedTreeMismatch``, which the caller turns into a refusal to vote.

    Returns ``(decision, diff_text)`` — the reviewed diff travels back out because it feeds the
    code_review artifact's change fingerprint after the vote."""
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
    # 409 guard (S2): a merge (>=2 parents) 409s the bare /patch, so route it through the
    # auto-merge-delta path instead. Emit the named signal so the otherwise-silent guard is
    # visible in the logs (fires ONLY on a merge — merge_detection logs is_merge for EVERY
    # change).
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


def _guard_decision_auth(cfg: ReceiverConfig) -> dict[str, Any] | None:
    """AC3 fail-closed guard: the decision-bearing Gerrit auth MUST be present before any
    dedup/clone/provider work. Returns an error-status dict (and emits the VOTER_ERROR marker)
    when the auth is missing/blank — the caller then casts NO vote and NEVER falls back to
    another principal — or ``None`` when a real token is present."""
    try:
        _startup.validate_decision_auth(cfg)
    except _startup.DecisionAuthError as exc:
        _voter_error(error=f"decision_auth: {exc}")
        return {"status": "error", "stage": "decision_auth"}
    return None


def _handle_retryable_gap(
    decision: dict[str, Any],
    store: DedupStore,
    cfg: ReceiverConfig,
    change_id: str,
    revision: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Handle a retryable coverage-gap decision (ticket 0347; ADR 0069).

    Returns ``(decision, deferred_result)``. When the decision's ``gap_reason`` is retryable
    and the per-revision attempt budget is NOT yet spent, the review is DEFERRED (cast NO vote,
    so the vote-less change stays visible to the backfill reconciler) and ``deferred_result`` is
    a ``{"status": "deferred", ...}`` dict the caller returns immediately. Otherwise
    ``deferred_result`` is ``None``: either the gap is not retryable (vote as-is) or the budget
    is spent (``decision`` is returned with the retries-exhausted note appended and the
    escalation VOTER_ERROR fired, so the caller casts the fail-closed -1)."""
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
        # Genuinely vote-less: nothing is posted to the change (a provisional comment would still
        # leave it vote-less, but the deferral must stay invisible to Gerrit entirely — the
        # reconciler pre-filters on "no LLM-Review vote").
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
    # Budget spent: cast the fail-closed -1 (message body gains the exhausted note; the
    # first-line tag vocabulary is unchanged) and fire the VOTER_ERROR marker so the
    # voter_errors alarm surface sees the escalation.
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


async def _review_and_vote(
    event: dict,
    *,
    config: ReceiverConfig | None = None,
    gerrit: GerritClient | None = None,
    dedup: DedupStore | None = None,
    force: bool = False,
    runtime: LLMRuntime | None = None,
) -> dict[str, Any]:
    """The review itself. Call :func:`review_and_vote` instead — it maintains the
    in-flight count the deploy loop reads."""
    cfg = config or ReceiverConfig.from_env()
    auth_error = _guard_decision_auth(cfg)
    if auth_error is not None:
        return auth_error
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

        # Merge detection (epic 88ab / S2): a merge revision (>= 2 parents) cannot use the
        # bare /patch (409) and must be reviewed on ONLY its auto-merge delta (R1). Detect
        # here — AFTER the existing-vote check, BEFORE any diff fetch — so the webhook,
        # reconciler-backfill, and /rerun paths all route through this SAME code (reconcile.py
        # needs no change). The extra commit GET is accepted overhead. A merge-path REST
        # failure (commit / files / mergelist / diff) is fail-closed as a -1 COVERAGE-GAP vote
        # (not a silent no-vote): the merge change is blocked AND visibly flagged as an infra
        # veto. ``decision`` is pre-set here on a commit-fetch failure so the review is skipped.
        decision: dict[str, Any] | None = None
        parent_count = -1  # -1 = commit fetch failed (unknown); logged with the vote below
        commit_message = ""  # the change's commit body (drives scope-intent); "" if unknown
        diff_text = ""  # the reviewed diff (drives the code_review artifact's change_fingerprint)
        try:
            commit_info = await asyncio.to_thread(gc.get_commit, change_id, revision)
            parent_count = len(commit_info.get("parents") or [])
            commit_message = str(commit_info.get("message") or "")
            is_merge = parent_count >= 2
            # Detection outcome is logged for EVERY change (not just merges): a merge that
            # Gerrit flattened to a single parent — or a genuine merge — is then unambiguous
            # from the logs, without which a mis-detection is silent (the failure mode that
            # made the S2 live smoke's first merge look like a non-merge).
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

        # Review: clone the ref, fetch the diff (merge vs non-merge path), run the adapter seam.
        # Skipped entirely when a merge-path infra gap already decided the vote above.
        if decision is None:
            try:
                # Per-change clone workdir. TemporaryDirectory resolves to the system temp
                # dir (tempfile.gettempdir(), typically /tmp) on the box's ROOT volume — not
                # the /var/gerrit data volume — so a large series of clones adds to root-disk
                # pressure, which the `rebar-root-disk-pressure` alarm watches (see the
                # "Disk-full triage" section of infra/runbooks/review-bot-ops.md).
                with tempfile.TemporaryDirectory(prefix="reviewbot-") as repo_root:
                    await asyncio.to_thread(
                        gc.clone_change_ref, info["change_number"], info["patchset_ref"], repo_root
                    )
                    decision, diff_text = await _decision_for_clone(
                        gc,
                        repo_root,
                        info,
                        is_merge=is_merge,
                        parent_count=parent_count,
                        commit_message=commit_message,
                        runtime=runtime,
                    )
            except adapter.ReviewedTreeMismatch as exc:
                # The cloned tree is provably NOT the revision this vote would attach to, so
                # NO vote is honest here — not even a -1, which would certify that this
                # revision was looked at. Refuse: surface the actionable message and leave the
                # change unsubmittable, exactly as a setup failure does.
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

        # post_review casts the vote AND anchors findings inline where they resolve to a real
        # revision path; a rejected comment retries message-only with an explicit notice rather
        # than silently dropping the text (bug lacquer-grotesque-urson). Its GerritError
        # contract is unchanged, so the handlers below are untouched.
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
            # A 409 "change is closed" is TERMINAL, not a retryable failure: the change was
            # merged/abandoned (a race past reconcile.py's open-status filter). Record it so
            # it is never retried, and do NOT emit a VOTER_ERROR / increment voter_errors — a
            # closed change needs no vote, so this is not an actionable fault (bug c943).
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
            # merge/parent_count on every vote: correlate a vote with the review path taken
            # (merge vs /patch) — the single most useful field when debugging "why did this
            # change get reviewed the way it did". parent_count == -1 means commit fetch failed.
            merge=is_merge,
            parent_count=parent_count,
        )
        # Token-usage observability (ticket clayish-basaltine-bug): record the review's LLM
        # token counts (from the verdict's coverage.metrics) to journald + CloudWatch. A
        # fail-closed review carries no verdict/metrics → nothing recorded. Best-effort.
        _review_metrics = ((decision.get("verdict") or {}).get("coverage") or {}).get(
            "metrics"
        ) or {}
        if _review_metrics:
            _emit_token_usage(change_id, revision, _review_metrics)
        # Data capture (story limestone-unethical-zebrafinch): emit a durable, change-scoped
        # code_review artifact into the AMBIENT tickets store (repo_root=None — NOT the temp code
        # clone, which is already deleted) and link it relates_to the change's trailer-cited
        # tickets. Best-effort: the vote is already cast, so this never fails the review.
        # emission via ``asyncio.to_thread`` so this SYNCHRONOUS, lock-held store write runs OFF
        # the event loop (c2ba). Run inline, it blocks the loop for the whole write, which
        # unenforces the drain and per-review ``wait_for`` bounds and stalls ``/health``. Every
        # other blocking Gerrit/store call in this coroutine is already offloaded the same way;
        # this was the lone on-loop residue.
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
