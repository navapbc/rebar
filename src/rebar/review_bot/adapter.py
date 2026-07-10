"""The formal b744 verdict→label seam (epic d251 / S4b; reimplemented by b744 / WS6).

This module holds the ONE function the rest of the receiver depends on for a
code-review decision:

    code_review_decision(diff_text, repo_root, ref) -> {decision, message, findings, coverage_gap}

**WS6 (this revision):** the contract is now implemented over the FOUR-PASS
``gate_dispatch.produce_code_review_verdict`` (the typed ``PASS``/``BLOCK``/``INDETERMINATE``
verdict) — a drop-in swap of the earlier single-pass ``review_code`` implementation, with NO
caller change (the voter still reads ``decision`` + ``message``). The four-pass gate's own
deterministic Pass-3 blocker decides PASS vs BLOCK (via ``criteria_routing.json`` thresholds), so
the adapter no longer applies a severity heuristic — ``ReceiverConfig.blocking_severities`` is now
vestigial for this path.

FORCE-ENABLE. The code-review gate is OFF by default (``verify.enable_code_review``), but voter
activation is itself the authoritative gate (a project is only reviewed once its receiver is
deployed + configured), so the adapter passes ``enabled=True`` — else every change would get the
inert disabled verdict. See ADR 0015.

DECISION RULE (fail-closed). PASS only for a genuine ``verdict == PASS`` with full coverage. A
real BLOCK (blocking findings), an INDETERMINATE (LLM outage), a fail-closed security-scanner
abstain, an inert-disabled verdict, or ANY exception → BLOCK. A BLOCK caused by a coverage gap
(infra) is marked ``coverage_gap=True`` and its message carries a DISTINCT tag from a real
finding, so an operator can tell an infra veto from a code veto.

SOURCE. The receiver has ALREADY cloned the change ref into ``repo_root`` (see
``gerrit_client.clone_change_ref`` / ``voter``); we review that working tree by passing
``repo_root`` (the security detectors scan the changed files there) + the fetched ``diff_text``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("rebar.review_bot.adapter")

__all__ = ["code_review_decision"]

#: The review-message first-line tag suffixes, keyed by reason. The message begins with
#: ``[LLM-Review: <suffix>]`` so an infra-failure ``-1`` (a coverage-gap sub-reason) is
#: unmistakable from a real-finding ``-1``. Documented vocabulary — asserted by a test.
_TAG_SUFFIXES: dict[str, str] = {
    "PASS": "PASS",
    "finding": "BLOCK — finding",
    "gate-disabled": "BLOCK — coverage-gap (gate-disabled)",
    "llm-unavailable": "BLOCK — coverage-gap (llm-unavailable)",
    "scanner": "BLOCK — coverage-gap (scanner)",
    "review-error": "BLOCK — coverage-gap (review-error)",
    "indeterminate": "BLOCK — coverage-gap (indeterminate)",
}

#: Map the four-pass kernel severity ({critical,major,minor,none}) to the finding vocabulary the
#: receiver logs ({critical,high,medium,info}) — mirrors the WS4 shim.
_KERNEL_TO_COMMON_SEVERITY = {
    "critical": "critical",
    "major": "high",
    "minor": "medium",
    "none": "info",
}


def _message_tag(
    reason: str, *, label: str = "LLM-Review", merge_commits: int | None = None
) -> str:
    """The first-line tag, e.g. ``[LLM-Review: PASS]``. For a merge change (``merge_commits``
    set) the merge-change variant is appended INSIDE the tag —
    ``[LLM-Review: PASS (merge-change, 3 integrated commits)]`` — reusing the strict
    ``_TAG_SUFFIXES[reason]`` lookup so the non-merge tag vocabulary is unchanged."""
    suffix = _TAG_SUFFIXES[reason]
    if merge_commits is not None:
        suffix += f" (merge-change, {merge_commits} integrated commit(s))"
    return f"[{label}: {suffix}]"


def _coverage_gap_reason(coverage: dict[str, Any]) -> str | None:
    """The coverage-gap sub-reason for a verdict's ``coverage`` block, or None if coverage was
    fully established. Order: inert **disabled** gate (``enabled is False``), then an **LLM outage**
    (``llm_unavailable``), then a **fail-closed security scanner abstain**. A scanner MATCH
    (``reason == 'detector-finding'``) is a real finding, NOT a coverage gap."""
    if coverage.get("enabled") is False:
        return "gate-disabled"
    if coverage.get("llm_unavailable"):
        return "llm-unavailable"
    for note in coverage.get("security_detectors") or []:
        if note.get("reason") == "fail-closed-abstain":
            return "scanner"
    return None


def _translate_findings(verdict: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize the verdict's blocking + advisory findings to the receiver's logged shape
    (``{severity, dimension, detail}``)."""
    out: list[dict[str, Any]] = []
    for f in (verdict.get("blocking") or []) + (verdict.get("advisory") or []):
        criteria = f.get("criteria") or []
        out.append(
            {
                "severity": _KERNEL_TO_COMMON_SEVERITY.get(
                    str(f.get("severity", "")).lower(), "info"
                ),
                "dimension": criteria[0] if criteria else "general",
                "detail": str(f.get("finding", "")).strip(),
            }
        )
    return out


def _summarize(reason: str, verdict: dict[str, Any]) -> str:
    coverage = verdict.get("coverage") or {}
    if reason == "PASS":
        n = len(verdict.get("advisory") or [])
        return "rebar code review passed." + (
            f" {n} advisory finding(s) (non-blocking)." if n else ""
        )
    if reason == "finding":
        blocking = verdict.get("blocking") or []
        lines = [f"rebar code review found {len(blocking)} blocking issue(s):"]
        for f in blocking[:10]:
            crit = (f.get("criteria") or ["general"])[0]
            detail = str(f.get("finding", "")).strip().replace("\n", " ")[:240]
            loc = f" [{f.get('location')}]" if f.get("location") else ""
            lines.append(f"- ({crit}) {detail}{loc}")
        return "\n".join(lines)
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
        # The FULL verdict is threaded up (story limestone-unethical-zebrafinch) so the voter can
        # emit a durable code_review artifact; {} on a fail-closed review-error (no artifact then).
        "verdict": verdict,
    }


def code_review_decision(
    diff_text: str,
    repo_root,
    ref: str,
    *,
    merge_commits: int | None = None,
    commit_message: str = "",
    change_id: str = "",
) -> dict[str, Any]:
    """Review ``diff_text`` (at the cloned ``repo_root``) via the four-pass gate and return
    ``{decision, message, findings, coverage_gap}``. PASS only for a genuine full-coverage PASS;
    a real BLOCK, an INDETERMINATE, a fail-closed scanner abstain, an inert-disabled verdict, or
    ANY exception → BLOCK (fail-closed). Signature + return shape are stable (the voter is
    unchanged); the four-pass gate owns the threshold.

    ``change_id`` (the Gerrit change) selects the ``change:<id>`` novelty keyspace for the
    region-gated floor (epic super-path-bag), so cross-patchset finding-memory is keyed on the
    CHANGE — spanning its revisions — the Gerrit analogue of the local ``session:<id>`` key."""
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

    try:
        verdict = produce_code_review_verdict(
            CodeReviewRequest(
                LLMConfig.from_env(repo_root=repo_root),
                diff_text=diff_text,
                commit_message=commit_message,  # drives the scope-intent overlay (default "")
                change_id=change_id,  # selects the change:<id> novelty keyspace (finding-memory)
                repo_root=repo_root,
                enabled=True,  # voter activation is the authoritative gate (ADR 0015)
            )
        )
    except Exception as exc:  # noqa: BLE001 — ANY review failure is fail-closed
        logger.warning("adapter: produce_code_review_verdict raised: %s", exc)
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
            "verdict": verdict,  # threaded up for the code_review artifact
        }
    if gap is not None:
        return _block(gap, verdict, merge_commits=merge_commits)
    if verdict.get("blocking"):
        return _block("finding", verdict, merge_commits=merge_commits)
    # Non-PASS (e.g. INDETERMINATE) with NO blocking findings and no detected coverage gap: the
    # review could not establish coverage — a coverage gap, NOT a code finding. Mapping this to
    # "finding" rendered the misleading "[LLM-Review: BLOCK — finding] ... 0 blocking issue(s):"
    # false -1 on a clean change (bug spy-luge-wool, observed on change 223).
    return _block("indeterminate", verdict, merge_commits=merge_commits)
