"""Merge-change review context assembly + its fail-closed markers (epic 88ab / S2).

Extracted from ``voter`` as a cohesive call-graph seam (the merge-change review path): a
merge revision (>= 2 parents) cannot use the bare ``/patch`` (409) and must be reviewed on
ONLY its auto-merge delta plus its integrated-commit list. This module owns that assembly and
the reason-tagged fail-closed markers/metrics it emits; the voter routes a merge review through
:func:`assemble_merge_diff` and turns any REST failure into a fail-closed ``-1`` coverage-gap
vote via :func:`merge_coverage_gap_decision`.

Bounded sequential REST fan-out per merge review: 1 files GET + 1 mergelist GET + N per-file
diff GETs, N bounded by ``DIFF_CHAR_CAP`` (per-file diffs are fetched only until the assembled
string reaches the cap). Any REST failure on this path fails CLOSED.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

from rebar.review_bot.gerrit_client import GerritClient, GerritError

logger = logging.getLogger("rebar.review_bot.voter")


def publish_merge_change_error_metric(reason: str) -> None:
    """Best-effort publish of ``rebar/host:review_bot_merge_change_errors`` (reason-tagged).
    The journald marker + the host probe (observability.sh) is the reliable path; in-container
    boto3 may not reach IMDS, so any failure is swallowed."""
    try:
        import boto3

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


def merge_change_error(event: str, reason: str, **fields: Any) -> None:
    """Structured ERROR marker for a merge-path REST failure. Writes a greppable
    ``MERGE_CHANGE_ERROR`` line to stderr (the host observability probe greps it to publish
    ``rebar/host:review_bot_merge_change_errors``, reason-tagged) with the specific event name
    (``merge_commit_error`` / ``merge_files_error`` / ``mergelist_fetch_error`` /
    ``merge_diff_error``) AND publishes the reason-tagged merge metric. The voter turns the
    failure into a fail-closed ``-1`` coverage-gap vote (see :func:`merge_coverage_gap_decision`)
    so the merge change is BLOCKED and visibly flagged as an INFRA veto, not silently no-voted."""
    record = {"event": event, "reason": reason, "timestamp": time.time(), **fields}
    line = "MERGE_CHANGE_ERROR " + json.dumps(record, default=str)
    logger.error(line)
    print(line, file=sys.stderr, flush=True)  # noqa: T201 — intentional journald marker
    publish_merge_change_error_metric(reason)


def merge_coverage_gap_decision(note: str) -> dict[str, Any]:
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


def assemble_merge_diff(
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
    from rebar.llm.code_review.assemble import (
        DIFF_CHAR_CAP,
        assemble_merge_change_context,
    )

    try:
        merge_files = gc.get_merge_files(change_id, revision)
    except GerritError as exc:
        merge_change_error("merge_files_error", "files", change_id=change_id, error=str(exc))
        raise
    try:
        mergelist = gc.get_mergelist(change_id, revision)
    except GerritError as exc:
        merge_change_error(
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
            merge_change_error(
                "merge_diff_error", "diff", change_id=change_id, file=path, error=str(exc)
            )
            raise
        text = render_diff_info(info)
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


def render_diff_info(info: dict) -> str:
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
