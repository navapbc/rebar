"""Durable ``code_review`` artifact emission for the review-bot (ticket 0347 split).

Extracted from ``voter.py`` along its existing call-graph seam — this cluster
(:func:`emit_code_review_artifact` + its swallowed-failure marker helpers) is called by the
voter exactly once, AFTER the vote is cast, and shares no state with the vote path: emission
is BEST-EFFORT (any failure is logged + counted, never raised) because the vote already
landed. Living apart also keeps ``voter.py`` under the module-size cap. ``voter`` re-exports
:func:`emit_code_review_artifact`, so existing importers and test monkeypatches of
``voter.emit_code_review_artifact`` are unaffected.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

logger = logging.getLogger("rebar.review_bot.artifact_emit")

__all__ = ["emit_code_review_artifact"]


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

        # Gerrit exposes one immutable revision under several equivalent
        # change identifiers. Reuse the revision's artifact across ingress
        # aliases while retaining the first readable Gerrit id in its title.
        artifact_id: str | None = None
        try:
            for t in rebar.list_tickets(ticket_type="code_review", repo_root=repo_root) or []:
                existing_title = str(t.get("title") or "")
                if existing_title.startswith("code-review: ") and existing_title.endswith(
                    f" @ {revision}"
                ):
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
