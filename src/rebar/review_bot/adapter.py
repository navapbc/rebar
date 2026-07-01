"""The formal b744 verdict→label seam (epic d251 / S4b).

This module holds the ONE function the rest of the receiver depends on for a
code-review decision:

    code_review_decision(diff_text, repo_root, ref) -> {decision, message, findings}

For the d251 *proven pipe* it implements that contract over the EXISTING single-pass
``rebar.llm.review_code(...)`` by mapping the returned ``review_result`` findings to a
PASS/BLOCK decision via a configured blocking-severity threshold. b744-WS6 will
REIMPLEMENT this same signature over ``gate_dispatch.produce_code_review_verdict``
(``PASS``/``BLOCK`` typed verdict) — drop-in, with no caller change — so the function
is kept deliberately small and free of receiver/Gerrit concerns.

DECISION RULE. BLOCK if any finding is at/above a configured blocking severity
(default ``{critical, high}``), else PASS. Fail-closed: any error, a missing result,
or an unparseable result → BLOCK (never let an unreviewed/uncertain change pass).

SOURCE MODE. We call ``review_code(..., source="local", ...)``: the receiver has
ALREADY cloned the change ref into ``repo_root`` (see ``gerrit_client.clone_change_ref``
/ ``voter``), so the reviewer must read THAT working tree. ``attested`` mode would
git-fetch ``origin`` and review the wrong (origin) state — wrong for a not-yet-merged
patchset whose ref lives only in the clone.
"""

from __future__ import annotations

import logging
from typing import Any

from rebar.review_bot.config import ReceiverConfig

logger = logging.getLogger("rebar.review_bot.adapter")

__all__ = ["code_review_decision"]


def _blocking(findings: list[dict[str, Any]], blocking_severities: frozenset[str]) -> list[dict]:
    """The subset of findings whose severity is in the blocking set (case-insensitive)."""
    blocked: list[dict] = []
    for f in findings:
        sev = str(f.get("severity", "")).strip().lower()
        if sev in blocking_severities:
            blocked.append(f)
    return blocked


def _summarize(findings: list[dict[str, Any]], blocked: list[dict[str, Any]]) -> str:
    """A short, human-readable message for the Gerrit robot comment."""
    if not findings:
        return "rebar code review: no findings."
    head = (
        f"rebar code review: {len(findings)} finding(s), "
        f"{len(blocked)} at/above the blocking threshold."
    )
    lines = [head]
    for f in (blocked or findings)[:10]:
        sev = str(f.get("severity", "info")).strip().lower()
        dim = str(f.get("dimension", "general")).strip()
        detail = str(f.get("detail", "")).strip().replace("\n", " ")
        if len(detail) > 240:
            detail = detail[:237] + "…"
        lines.append(f"- [{sev}] ({dim}) {detail}")
    return "\n".join(lines)


def code_review_decision(
    diff_text: str,
    repo_root,
    ref: str,
    *,
    config: ReceiverConfig | None = None,
) -> dict[str, Any]:
    """Review ``diff_text`` and return ``{decision, message, findings}``.

    ``decision`` is ``"PASS"`` or ``"BLOCK"``. PASS only when ``review_code`` returns a
    parseable result whose findings are ALL below the configured blocking severity;
    every other path (blocking finding, exception, no/empty result, bad shape) is
    ``"BLOCK"`` — fail-closed. ``findings`` is the (possibly empty) normalized findings
    list; ``message`` is a short summary safe to post as a Gerrit robot comment.

    This is the seam b744-WS6 reimplements over ``produce_code_review_verdict`` — keep
    the signature + return shape stable.
    """
    cfg = config or ReceiverConfig.from_env()

    try:
        # Imported lazily: the [agents] extra (and its heavy deps) must not load merely
        # because the receiver package was imported — only when a review actually runs.
        from rebar.llm import review_code
    except Exception as exc:  # noqa: BLE001 — a missing/broken extra is a fail-closed BLOCK
        logger.warning("adapter: review_code import failed: %s", exc)
        return {
            "decision": "BLOCK",
            "message": f"rebar code review unavailable (import failed: {exc}); blocking.",
            "findings": [],
        }

    try:
        result = review_code(
            diff_text=diff_text,
            source="local",
            repo_root=repo_root,
            ref=ref,
        )
    except Exception as exc:  # noqa: BLE001 — ANY review failure is fail-closed
        logger.warning("adapter: review_code raised: %s", exc)
        return {
            "decision": "BLOCK",
            "message": f"rebar code review failed ({exc}); blocking (fail-closed).",
            "findings": [],
        }

    if not isinstance(result, dict):
        return {
            "decision": "BLOCK",
            "message": "rebar code review returned no usable result; blocking (fail-closed).",
            "findings": [],
        }

    findings = result.get("findings")
    if not isinstance(findings, list):
        return {
            "decision": "BLOCK",
            "message": "rebar code review result had no findings list; blocking (fail-closed).",
            "findings": [],
        }

    blocked = _blocking(findings, cfg.blocking_severities)
    decision = "BLOCK" if blocked else "PASS"
    message = _summarize(findings, blocked)
    return {"decision": decision, "message": message, "findings": findings}
