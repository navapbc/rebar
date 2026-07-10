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


def _publish_artifact_emit_error_metric() -> None:
    """Best-effort publish of ``rebar/host:review_bot_artifact_emit_errors``, mirroring
    :func:`_publish_voter_error_metric`. The journald marker + the host probe is the reliable
    path; in-container boto3 may not reach IMDS, so any failure is swallowed."""
    try:
        import boto3  # noqa: PLC0415 — optional, lazy: only on a best-effort error path

        boto3.client("cloudwatch").put_metric_data(
            Namespace="rebar/host",
            MetricData=[
                {"MetricName": "review_bot_artifact_emit_errors", "Value": 1, "Unit": "Count"}
            ],
        )
    except Exception:  # noqa: BLE001 — IMDS hop limit / no creds / offline: journald is the fallback
        pass


def _artifact_emit_error(**fields: Any) -> None:
    """Greppable marker for a SWALLOWED code_review artifact-emission failure (bug
    desirous-judicial-hogget). Emission is best-effort — the vote is already cast — but a
    write-dead tickets store (e.g. a fresh single-branch clone lacking ``.env-id``) would
    otherwise be a SILENT no-op. Emit a distinct ``ARTIFACT_EMIT_ERROR`` line to stderr
    (journald) + a countable metric so the write-dead store is detectable in logs, WITHOUT
    changing the continue-don't-crash behaviour."""
    record = {"event": "ARTIFACT_EMIT_ERROR", "timestamp": time.time(), **fields}
    line = "ARTIFACT_EMIT_ERROR " + json.dumps(record, default=str)
    logger.warning(line)
    print(line, file=sys.stderr, flush=True)  # noqa: T201 — intentional journald marker
    _publish_artifact_emit_error_metric()


# ── merge-change review path (epic 88ab / S2) ────────────────────────────────
# Bounded sequential REST fan-out per merge review: 1 commit GET (detection) + 1 files GET
# + 1 mergelist GET + N per-file diff GETs, N bounded by DIFF_CHAR_CAP (per-file diffs are
# fetched only until the assembled string reaches the cap). Latency budget: the extra
# per-review overhead is a small constant (~3 REST round-trips) plus at most a handful of
# per-file diff GETs before the char cap short-circuits the loop — well inside the review's
# existing multi-second LLM budget. Any REST failure on this path fails CLOSED.


def _publish_merge_change_error_metric(reason: str) -> None:
    """Best-effort publish of ``rebar/host:review_bot_merge_change_errors`` (reason-tagged),
    mirroring :func:`_publish_voter_error_metric`. The journald marker + the host probe
    (observability.sh) is the reliable path; in-container boto3 may not reach IMDS, so any
    failure is swallowed."""
    try:
        import boto3  # noqa: PLC0415 — optional, lazy: only on a fail-closed error path

        boto3.client("cloudwatch").put_metric_data(
            Namespace="rebar/host",
            MetricData=[
                {
                    "MetricName": "review_bot_merge_change_errors",
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "reason", "Value": reason}],
                }
            ],
        )
    except Exception:  # noqa: BLE001 — IMDS hop limit / no creds / offline: journald is the fallback
        pass


def _merge_change_error(event: str, reason: str, **fields: Any) -> None:
    """Structured ERROR marker for a merge-path REST failure. Writes a greppable
    ``MERGE_CHANGE_ERROR`` line to stderr (the host observability probe greps it to publish
    ``rebar/host:review_bot_merge_change_errors``, reason-tagged) with the specific event name
    (``merge_commit_error`` / ``merge_files_error`` / ``mergelist_fetch_error`` /
    ``merge_diff_error``) AND publishes the reason-tagged merge metric. The voter turns the
    failure into a fail-closed ``-1`` coverage-gap vote (see :func:`_merge_coverage_gap_decision`)
    so the merge change is BLOCKED and visibly flagged as an INFRA veto, not silently no-voted."""
    record = {"event": event, "reason": reason, "timestamp": time.time(), **fields}
    line = "MERGE_CHANGE_ERROR " + json.dumps(record, default=str)
    logger.error(line)
    print(line, file=sys.stderr, flush=True)  # noqa: T201 — intentional journald marker
    _publish_merge_change_error_metric(reason)


def _merge_coverage_gap_decision(note: str) -> dict[str, Any]:
    """A fail-closed BLOCK decision for a merge-path infra failure — cast as a ``-1`` with a
    coverage-gap tag so the merge change is BLOCKED and the operator sees an INFRA veto (the
    merge review could not run), NOT a code finding. Mirrors the adapter's coverage-gap shape;
    the tag carries the ``coverage-gap`` marker so it is unmistakable from a real ``-1``."""
    return {
        "decision": "BLOCK",
        "message": (
            "[LLM-Review: BLOCK — coverage-gap (merge-review)]\n"
            f"rebar could not review the merge change — {note}. Fail-closed veto "
            "(infrastructure, not your code); re-run once the merge-path is healthy."
        ),
        "findings": [],
        "coverage_gap": True,
    }


def emit_code_review_artifact(
    decision: dict[str, Any],
    *,
    change_id: str,
    revision: str,
    commit_message: str,
    diff_text: str,
    repo_root: str | None = None,
) -> str | None:
    """Emit a durable, change-scoped ``code_review`` artifact ticket for a completed review and link
    it ``relates_to`` every rebar ticket named in the change's ``rebar-ticket:`` trailers (story
    limestone-unethical-zebrafinch). Returns the artifact ticket id (or None if nothing was
    emitted). BEST-EFFORT: any failure is logged and swallowed — the vote is already cast, so
    artifact emission must NEVER fail the review. Idempotent per ``(change_id, revision)``: a
    re-review of the same revision reuses the existing artifact rather than duplicating."""
    verdict = decision.get("verdict") or {}
    if not verdict:
        return None  # a fail-closed review-error carries no verdict → nothing durable to persist
    try:
        import rebar
        from rebar import config as _config
        from rebar._commands.verify_commit import extract_ticket_refs
        from rebar._engine_support.resolver import resolve_ticket_id
        from rebar.llm.code_review import sidecar
        from rebar.llm.code_review.assemble import changed_from_diff

        changed_files = changed_from_diff(diff_text or "")
        change_fp = sidecar.change_fingerprint(change_id, revision, changed_files, diff_text or "")
        title = f"code-review: {change_id} @ {revision}"

        # Idempotency per (change_id, revision): reuse an existing artifact with the same title.
        artifact_id: str | None = None
        try:
            for t in rebar.list_tickets(ticket_type="code_review", repo_root=repo_root) or []:
                if str(t.get("title") or "") == title:
                    artifact_id = str(t.get("ticket_id") or t.get("id") or "") or None
                    break
        except Exception:  # noqa: BLE001 — a lookup failure just means we create a fresh artifact
            artifact_id = None

        if not artifact_id:
            created = rebar.create_ticket(
                "code_review",
                title,
                description=(
                    f"Code-review artifact for Gerrit change {change_id} (revision {revision}). "
                    f"Decision: {decision.get('decision')}. change_fingerprint={change_fp}."
                ),
                return_alias=True,
                repo_root=repo_root,
            )
            artifact_id = str(created["id"] if isinstance(created, dict) else created)

        sidecar.emit(
            verdict,
            target_ticket=artifact_id,
            repo_root=repo_root,
            change_id=change_id,
            revision=revision,
            change_fp=change_fp,
        )

        # Trailer resolution → relates_to links. RESOLVABLE → link; UNRESOLVED/FOREIGN → WARN, skip.
        try:
            tracker: str | None = str(_config.tracker_dir(repo_root))
        except Exception:  # noqa: BLE001 — an unlocatable store ⇒ no links (inert/safe)
            tracker = None
        refs = extract_ticket_refs(commit_message or "")
        linked = 0
        for ref in refs:
            resolved = None
            if tracker:
                try:
                    resolved = resolve_ticket_id(ref, tracker)
                except Exception:  # noqa: BLE001 — a bad candidate is treated as unresolved
                    resolved = None
            if resolved:
                try:
                    rebar.link(artifact_id, resolved, "relates_to", repo_root=repo_root)
                    linked += 1
                except Exception:  # noqa: BLE001 — one failed link never aborts the rest
                    logger.warning(
                        "code_review artifact %s: relates_to link to %s failed",
                        artifact_id,
                        resolved,
                        exc_info=True,
                    )
            else:
                logger.warning(
                    "code_review artifact %s: unresolved rebar-ticket trailer %r (change %s/%s)",
                    artifact_id,
                    ref,
                    change_id,
                    revision,
                )
        logger.info(
            "code_review artifact %s: linked %d/%d trailer refs", artifact_id, linked, len(refs)
        )
        return artifact_id
    except Exception as exc:  # noqa: BLE001 — artifact emission is best-effort; never fail the vote
        # NON-silent (bug desirous-judicial-hogget / d220): a write-dead tickets store — e.g. a
        # fresh single-branch clone missing `.env-id` (converged by
        # infra/scripts/reviewbot-ensure-tickets.sh) — otherwise makes emission a SILENT no-op.
        # Emit a distinct, greppable ARTIFACT_EMIT_ERROR marker + a countable metric so it is
        # detectable in logs. The vote is already cast, so we STILL continue-don't-crash.
        _artifact_emit_error(change_id=change_id, revision=revision, error=str(exc))
        logger.warning("code_review artifact emission failed; continuing", exc_info=True)
        return None


def _assemble_merge_diff(
    gc: GerritClient, change_id: str, revision: str
) -> tuple[str, int, dict[str, Any]]:
    """Assemble the merge-change review context (auto-merge delta + integrated-commit list)
    for a MERGE revision and return ``(diff_text, integrated_commit_count, stats)``. NEVER
    calls the bare ``/patch`` (409 on a merge). Per-file diffs are fetched only until the
    assembled string reaches ``DIFF_CHAR_CAP`` (bounds the sequential REST fan-out). ANY REST
    failure raises ``GerritError`` (the caller fails closed).

    ``stats`` is a small dict the caller logs on ``merge_change_review`` so an operator can
    debug WHAT the reviewer actually saw without re-running: how many real (non-magic)
    conflict files the auto-merge had, how many diffs were fetched before the cap, whether the
    auto-merge delta was empty (a clean merge), whether the REST fan-out was truncated by the
    char cap, and the assembled context size."""
    from rebar.llm.code_review.assemble import (  # noqa: PLC0415 — lazy (mirror adapter)
        DIFF_CHAR_CAP,
        assemble_merge_change_context,
    )

    try:
        merge_files = gc.get_merge_files(change_id, revision)
    except GerritError as exc:
        _merge_change_error("merge_files_error", "files", change_id=change_id, error=str(exc))
        raise
    try:
        mergelist = gc.get_mergelist(change_id, revision)
    except GerritError as exc:
        _merge_change_error(
            "mergelist_fetch_error", "mergelist", change_id=change_id, error=str(exc)
        )
        raise

    # Fetch per-file diffs for REAL files (skip magic pseudo-paths) until the combined cap.
    real_files = [p for p in merge_files if p not in GerritClient.MAGIC_PATHS]
    file_diffs: dict[str, str] = {}
    running = 0
    cap_hit = False
    for path in real_files:
        if running >= DIFF_CHAR_CAP:
            cap_hit = True  # remaining files skipped — the reviewer sees a truncated fan-out
            break
        try:
            info = gc.get_file_diff(change_id, path, revision)
        except GerritError as exc:
            _merge_change_error(
                "merge_diff_error", "diff", change_id=change_id, file=path, error=str(exc)
            )
            raise
        text = _render_diff_info(info)
        file_diffs[path] = text
        running += len(text)
    diff_text = assemble_merge_change_context(merge_files, file_diffs, mergelist)
    stats = {
        "real_files": len(real_files),
        "files_fetched": len(file_diffs),
        "auto_diff_empty": len(file_diffs) == 0,
        "diff_cap_hit": cap_hit,
        "assembled_chars": len(diff_text),
    }
    return diff_text, len(mergelist), stats


def _render_diff_info(info: dict) -> str:
    """Flatten a Gerrit ``DiffInfo`` (``content`` list of ``{ab|a|b}`` segments) into unified
    diff-ish text for the reviewer. Only changed segments (``a``/``b``) are emitted with
    ``-``/``+`` prefixes; unchanged ``ab`` context is summarized to keep the delta focused."""
    lines: list[str] = []
    for seg in info.get("content") or []:
        if "ab" in seg:
            n = len(seg["ab"])
            lines.append(f"  … {n} unchanged line(s) …")
            continue
        for ln in seg.get("a") or []:
            lines.append(f"-{ln}")
        for ln in seg.get("b") or []:
            lines.append(f"+{ln}")
    return "\n".join(lines)


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

        # Merge detection (epic 88ab / S2): a merge revision (>= 2 parents) cannot use the
        # bare /patch (409) and must be reviewed on ONLY its auto-merge delta (R1). Detect
        # here — AFTER the existing-vote check, BEFORE any diff fetch — so the webhook,
        # reconciler-backfill, and /rerun paths all route through this SAME code (reconcile.py
        # needs no change). The extra commit GET is accepted overhead. A merge-path REST
        # failure (commit / files / mergelist / diff) is fail-closed as a -1 COVERAGE-GAP vote
        # (not a silent no-vote): the merge change is blocked AND visibly flagged as an infra
        # veto. ``decision`` is pre-set here on a commit-fetch failure so the review is skipped.
        decision: dict[str, Any] | None = None
        merge_commits: int | None = None
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
                with tempfile.TemporaryDirectory(prefix="reviewbot-") as repo_root:
                    await asyncio.to_thread(
                        gc.clone_change_ref, info["change_number"], info["patchset_ref"], repo_root
                    )
                    if is_merge:
                        # 409 guard (S2): a merge (>=2 parents) 409s the bare /patch, so route it
                        # through the auto-merge-delta path instead. Emit the named signal so the
                        # otherwise-silent guard is visible in the logs (fires ONLY on a merge —
                        # merge_detection above logs is_merge for EVERY change).
                        _emit(
                            logging.INFO,
                            "merge_change_409_guard",
                            change_id=change_id,
                            revision_id=revision,
                            parent_count=parent_count,
                        )
                        # ONLY the auto-merge delta + integrated-commit context — never /patch.
                        # A merge-path REST failure here is a fail-closed -1 coverage-gap (the
                        # clone succeeded, so the vote POST below can still reach Gerrit).
                        try:
                            diff_text, merge_commits, stats = await asyncio.to_thread(
                                _assemble_merge_diff, gc, change_id, revision
                            )
                        except GerritError as exc:
                            decision = _merge_coverage_gap_decision(
                                f"merge context assembly failed: {exc}"
                            )
                        else:
                            # Log WHAT the reviewer saw (context stats) so a merge review can be
                            # debugged from logs alone: empty auto-merge delta, a truncated REST
                            # fan-out, or an unexpected file/commit count are all visible here.
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
                                merge_commits=merge_commits,
                                change_id=change_id,  # change:<id> novelty keyspace
                            )
                    else:
                        diff_text = await asyncio.to_thread(gc.get_patch, change_id, revision)
                        decision = await asyncio.to_thread(
                            adapter.code_review_decision,
                            diff_text,
                            repo_root,
                            info["patchset_ref"],
                            commit_message=commit_message,  # scope-intent overlay (non-merge path)
                            change_id=change_id,  # change:<id> novelty keyspace (finding-memory)
                        )
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

        # Map decision → vote value. BLOCK (incl. adapter fail-closed) → block value;
        # PASS → max value. A MAX is cast ONLY on an explicit PASS.
        is_pass = decision.get("decision") == "PASS"
        value = cfg.llm_review_max_value if is_pass else cfg.llm_review_block_value
        message = decision.get("message") or "rebar code review."

        try:
            http_status = await asyncio.to_thread(gc.post_vote, change_id, revision, value, message)
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
            # merge/parent_count on every vote: correlate a vote with the review path taken
            # (merge vs /patch) — the single most useful field when debugging "why did this
            # change get reviewed the way it did". parent_count == -1 means commit fetch failed.
            merge=is_merge,
            parent_count=parent_count,
        )
        # Data capture (story limestone-unethical-zebrafinch): emit a durable, change-scoped
        # code_review artifact into the AMBIENT tickets store (repo_root=None — NOT the temp code
        # clone, which is already deleted) and link it relates_to the change's trailer-cited
        # tickets. Best-effort: the vote is already cast, so this never fails the review.
        emit_code_review_artifact(
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
