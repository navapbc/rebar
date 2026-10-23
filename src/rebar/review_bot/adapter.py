"""Translate four-pass code review verdicts into Gerrit decisions.
The receiver force-enables dispatch and reviews the cloned tree and diff.
Only full-coverage PASS maps to PASS. Findings and coverage gaps remain
distinct. A proven reviewed-tree mismatch raises and prevents a vote.
The gate owns blocking thresholds and finding-memory by change ID.
``ReceiverConfig.blocking_severities`` has since been removed entirely."""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rebar.review_bot.finding_publish import render_findings_block

if TYPE_CHECKING:
    from rebar.llm.auth import LLMRuntime

logger = logging.getLogger("rebar.review_bot.adapter")

__all__ = [
    "RETRYABLE_GAP_REASONS",
    "ReviewedTreeMismatch",
    "append_retries_exhausted_note",
    "code_review_decision",
]

#: The review-message first-line tag suffixes, keyed by reason. The message begins with
#: ``[LLM-Review: <suffix>]`` so an infra-failure ``-1`` (a coverage-gap sub-reason) is
#: unmistakable from a real-finding ``-1``. Documented vocabulary — asserted by a test.
_TAG_SUFFIXES: dict[str, str] = {
    "PASS": "PASS",
    "finding": "BLOCK — finding",
    "gate-disabled": "BLOCK — coverage-gap (gate-disabled)",
    "llm-unavailable": "BLOCK — coverage-gap (llm-unavailable)",
    "input-rejected": "BLOCK — coverage-gap (input-rejected)",
    "scanner": "BLOCK — coverage-gap (scanner)",
    "review-error": "BLOCK — coverage-gap (review-error)",
    "low-disk": "BLOCK — coverage-gap (low-disk)",
    "indeterminate": "BLOCK — coverage-gap (indeterminate)",
}

#: Retryable interruptions defer without a vote so backfill can retry.
#: Completed indeterminate reviews and findings are not interruptions.
#: Merge coverage gaps keep their immediate fail-closed vote.
RETRYABLE_GAP_REASONS = frozenset(
    {"review-error", "llm-unavailable", "scanner", "gate-disabled", "low-disk"}
)


def _message_tag(
    reason: str, *, label: str = "LLM-Review", merge_commits: int | None = None
) -> str:
    """Return the first-line verdict tag, including integrated commit count
    for a merge change."""
    suffix = _TAG_SUFFIXES[reason]
    if merge_commits is not None:
        suffix += f" (merge-change, {merge_commits} integrated commit(s))"
    return f"[{label}: {suffix}]"


def _coverage_gap_reason(coverage: dict[str, Any]) -> str | None:
    """Return disabled, LLM outage, or scanner abstain when coverage failed.
    A scanner finding is a code finding, not a coverage gap."""
    if coverage.get("low_disk"):
        return "low-disk"
    if coverage.get("enabled") is False:
        return "gate-disabled"
    if coverage.get("llm_unavailable"):
        return "llm-unavailable"
    if coverage.get("input_rejected"):
        return "input-rejected"
    for note in coverage.get("security_detectors") or []:
        if note.get("reason") == "fail-closed-abstain":
            return "scanner"
    return None


def _translate_findings(verdict: dict[str, Any]) -> list[dict[str, Any]]:
    """Map blocking and advisory findings to receiver records with dimension,
    detail, location, and any carried-patchset standing marker."""
    out: list[dict[str, Any]] = []
    for is_blocking, findings in (
        (True, verdict.get("blocking") or []),
        (False, verdict.get("advisory") or []),
    ):
        for f in findings:
            criteria = f.get("criteria") or []
            item = {
                "blocking": is_blocking,
                "dimension": criteria[0] if criteria else "general",
                "detail": str(f.get("finding", "")).strip(),
                "location": f.get("location") or "",
            }
            # Carry standing only for findings retained from an earlier patchset.
            standing = f.get("standing")
            if isinstance(standing, dict):
                item["standing"] = dict(standing)
            out.append(item)
    return out


def _summarize(reason: str, verdict: dict[str, Any]) -> str:
    coverage = verdict.get("coverage") or {}
    advisory = verdict.get("advisory") or []
    if reason == "PASS":
        if not advisory:
            return "rebar code review passed."
        # The finding TEXT, not just its count (bug lacquer-grotesque-urson): a count alone left
        # advisories unreadable on the change, so nobody could judge which criteria to promote
        # to blocking.
        return "rebar code review passed. " + render_findings_block(advisory, kind="advisory")
    if reason == "finding":
        blocking = verdict.get("blocking") or []
        block = render_findings_block(blocking, kind="blocking") or (
            "rebar code review found 0 blocking issue(s):"
        )
        # Advisories ride along on the BLOCK path too — they were dropped here as well.
        tail = render_findings_block(advisory, kind="advisory")
        return f"{block}\n\n{tail}" if tail else block
    # coverage-gap sub-reasons — name the gap; it is infra, not "bad code".
    if reason == "scanner":
        gaps = "; ".join(
            f"{n.get('criterion')} ({', '.join(n.get('abstain_reasons') or [])})"
            for n in coverage.get("security_detectors") or []
            if n.get("reason") == "fail-closed-abstain"
        )
        detail = f"a security scanner could not run: {gaps}"
    else:
        llm_err = coverage.get("llm_error", "outage")
        detail = {
            "gate-disabled": "the code-review gate is disabled — cannot certify",
            "llm-unavailable": f"the review LLM was unavailable ({llm_err})",
            "input-rejected": "the review input was rejected (deterministic — oversized or "
            "policy-blocked; re-running the same input will be rejected identically)",
            "low-disk": "the review host is below the hard free-space admission floor",
            "indeterminate": "the review returned INDETERMINATE with no blocking findings "
            "(could not establish coverage — not a code finding)",
        }.get(reason, "the code review could not run")
    return (
        f"rebar code review coverage gap — {detail}. Fail-closed veto (infrastructure, not your "
        "code); re-run once the gate/scanner is healthy."
    )


def _block(
    reason: str, verdict: dict[str, Any], *, merge_commits: int | None = None
) -> dict[str, Any]:
    tag = _message_tag(reason, merge_commits=merge_commits)
    return {
        "decision": "BLOCK",
        "message": f"{tag}\n{_summarize(reason, verdict)}",
        "findings": _translate_findings(verdict),
        "coverage_gap": reason != "finding",
        # Machine-readable sub-reason (ticket 0347): drives the voter's defer-vs-vote split.
        # None for a real finding — only coverage gaps carry a gap_reason.
        "gap_reason": reason if reason != "finding" else None,
        # The FULL verdict is threaded up (story limestone-unethical-zebrafinch) so the voter can
        # emit a durable code_review artifact; {} on a fail-closed review-error (no artifact then).
        "verdict": verdict,
    }


class ReviewedTreeMismatch(RuntimeError):
    """Raised for a proven mismatch between the reviewed checkout and the
    revision that would receive the vote."""


#: Wall-clock bound (seconds) on the single read-only ``rev-parse`` used to identify the tree.
_REV_PARSE_TIMEOUT = 30

#: Compare only commit names with at least 7 hexadecimal digits.
#: Others are unverifiable, not proven mismatches.
_SHA_RE = re.compile(r"\A[0-9a-f]{7,64}\Z")


def _resolve_reviewed_head(repo_root: str | Path) -> str | None:
    """Return the checked-out HEAD or None without a resolvable git identity.
    The later tickets fetch changes FETCH_HEAD, not the reviewed tree."""
    try:
        # raw-git-ok: read-only `rev-parse` against a disposable review clone, not the tracker
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=_REV_PARSE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _assert_reviewed_tree(repo_root: str | Path, ref: str, revision: str) -> None:
    """Raise only when comparable checked-out and voted revisions differ.
    Missing or invalid commit names cannot establish a mismatch, so warn
    and return. Clone failure is handled upstream."""
    if not revision:
        return
    resolved = _resolve_reviewed_head(repo_root)
    if resolved is None:
        logger.warning(
            "adapter: cannot identify the reviewed tree at %s — binding not checked", repo_root
        )
        return
    left, right = resolved.strip().lower(), revision.strip().lower()
    if not _SHA_RE.match(left) or not _SHA_RE.match(right):
        logger.warning(
            "adapter: uncomparable commit names (%r vs %r) — binding not checked", left, right
        )
        return
    # Prefix-tolerant so an abbreviated name on either side still matches its full form; the
    # 7-hex floor above is what keeps that from matching unrelated commits.
    if left.startswith(right) or right.startswith(left):
        return
    raise ReviewedTreeMismatch(
        f"the reviewed tree is not the voted revision: the clone of {ref} at {repo_root} is "
        f"checked out at {left}, but the vote would attach to {right}. Refusing to vote — "
        "reviewing one commit and certifying another cannot be made safe. Check that the clone "
        "directory was fresh and that the fetch of that ref completed, then re-trigger."
    )


def code_review_decision(
    diff_text: str,
    repo_root: str | Path,
    ref: str,
    *,
    revision: str = "",
    merge_commits: int | None = None,
    commit_message: str = "",
    change_id: str = "",
    runtime: LLMRuntime | None = None,
) -> dict[str, Any]:
    """Review the cloned diff through the four-pass gate and return decision,
    message, findings, and coverage_gap. Only full-coverage PASS passes.
    Other completed verdicts and ordinary failures block, with coverage
    gaps distinguished from findings. A proven tree mismatch raises before
    review so the caller casts no vote. change_id keys finding memory across
    patchsets."""
    _assert_reviewed_tree(repo_root, ref, revision)
    try:
        # Lazily imported: the [agents] extra (heavy) must not load merely because the receiver
        # package was imported — only when a review actually runs.
        from rebar.llm.config import LLMConfig
        from rebar.llm.workflow.gate_dispatch import (
            CodeReviewRequest,
            produce_code_review_verdict,
        )
    except Exception as exc:  # noqa: BLE001 — a missing/broken extra is a fail-closed BLOCK
        logger.warning("adapter: gate import failed: %s", exc)
        return _block("review-error", {}, merge_commits=merge_commits)

    # Forward a composed runtime into the runner and request.
    # Without one, retain the ambient runner path.
    runner = None
    if runtime is not None:
        try:
            from rebar.llm.runner import get_runner

            runner = get_runner(LLMConfig.from_env(repo_root=repo_root), runtime=runtime)
        except Exception as exc:  # noqa: BLE001 — a runner build failure is a fail-closed BLOCK
            logger.warning("adapter: runner build failed: %s", exc)
            return _block("review-error", {}, merge_commits=merge_commits)

    try:
        verdict = produce_code_review_verdict(
            CodeReviewRequest(
                LLMConfig.from_env(repo_root=repo_root),
                diff_text=diff_text,
                commit_message=commit_message,  # drives the scope-intent overlay (default "")
                change_id=change_id,  # selects the change:<id> novelty keyspace (finding-memory)
                repo_root=repo_root,
                enabled=True,  # voter activation is the authoritative gate (ADR 0015)
                runner=runner,  # forwarded composed runtime (None → ambient, unchanged)
            )
        )
    except Exception as exc:  # noqa: BLE001 — ANY review failure is fail-closed unless typed
        from rebar._snapshot.janitor import SnapshotLowDiskError

        logger.warning("adapter: produce_code_review_verdict raised: %s", exc)
        if isinstance(exc, SnapshotLowDiskError):
            space = exc.space
            return _block(
                "low-disk",
                {
                    "verdict": "BLOCK",
                    "blocking": [],
                    "advisory": [],
                    "coverage": {
                        "low_disk": True,
                        "low_disk_path": str(space.path),
                        "low_disk_free_bytes": space.free_bytes,
                        "low_disk_min_bytes": space.min_free_bytes,
                    },
                },
                merge_commits=merge_commits,
            )
        return _block("review-error", {}, merge_commits=merge_commits)

    if not isinstance(verdict, dict) or "verdict" not in verdict:
        return _block("review-error", {}, merge_commits=merge_commits)

    gap = _coverage_gap_reason(verdict.get("coverage") or {})
    if verdict.get("verdict") == "PASS" and gap is None:
        tag = _message_tag("PASS", merge_commits=merge_commits)
        return {
            "decision": "PASS",
            "message": f"{tag}\n{_summarize('PASS', verdict)}",
            "findings": _translate_findings(verdict),
            "coverage_gap": False,
            "gap_reason": None,
            "verdict": verdict,  # threaded up for the code_review artifact
        }
    if gap is not None:
        return _block(gap, verdict, merge_commits=merge_commits)
    if verdict.get("blocking"):
        return _block("finding", verdict, merge_commits=merge_commits)
    # A non-PASS without blocking findings or a detected gap still lacks
    # coverage. Report that gap instead of inventing a code finding.
    return _block("indeterminate", verdict, merge_commits=merge_commits)


def append_retries_exhausted_note(decision: dict[str, Any], attempts: int) -> dict[str, Any]:
    """Append the exhausted-budget explanation to a coverage-gap message
    without changing its first-line verdict tag."""
    out = dict(decision)
    out["message"] = (
        f"{decision.get('message', '')}\n\n"
        f"Automatic retries exhausted ({attempts} attempt(s)) — casting the fail-closed -1. "
        "A contributor re-trigger or a new patchset restarts the retry budget."
    )
    return out
