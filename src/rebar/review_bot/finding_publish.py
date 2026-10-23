"""Publish review findings in Gerrit messages and optional inline comments.
The message enumerates findings even when file anchors are unavailable or
inline posting fails. Valid revision paths may also receive inline comments.
The adapter shares this module's rendering for blocking and advisory text."""

from __future__ import annotations

import logging
from typing import Any

from rebar.review_bot.gerrit_client import GerritError

logger = logging.getLogger("rebar.review_bot.finding_publish")

__all__ = [
    "MAX_DETAIL_CHARS",
    "MAX_ITEMS",
    "build_inline_comments",
    "parse_anchor",
    "partition_findings",
    "post_review",
    "render_findings_block",
]

#: Keep shared message-detail and item-count limits for both finding kinds.
MAX_DETAIL_CHARS = 240
MAX_ITEMS = 10

#: Headline for each enumerated block, by finding kind.
_HEADERS = {
    "blocking": "rebar code review found {n} blocking issue(s):",
    "advisory": "{n} advisory finding(s) (non-blocking):",
}


def parse_anchor(location: Any) -> tuple[str, int | None] | None:
    """Parse a finding location into path and optional line.
    Use the same rule as the review-result parser without importing its
    agents dependency."""
    if not isinstance(location, str) or not location.strip():
        return None
    s = location.strip()
    path, sep, rest = s.rpartition(":")
    if sep and path:
        head = rest.split("-", 1)[0].split(",", 1)[0].strip()
        try:
            return (path, int(head))
        except ValueError:
            return (s, None)
    return (s, None)


def partition_findings(
    findings: list[dict[str, Any]], revision_paths: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separate findings with a numbered path in the revision file map from
    those that belong in the message body. Invalid paths cannot break a vote."""
    anchorable: list[dict[str, Any]] = []
    anchorless: list[dict[str, Any]] = []
    for f in findings:
        anchor = parse_anchor(f.get("location"))
        if anchor is not None and anchor[1] is not None and anchor[0] in revision_paths:
            anchorable.append(f)
        else:
            anchorless.append(f)
    return anchorable, anchorless


# Accept raw kernel findings and translated receiver findings when
# rendering message text or inline comments.
def _detail_of(finding: dict[str, Any]) -> str:
    raw = finding.get("finding") or finding.get("detail") or ""
    full = str(raw).strip().replace("\n", " ")
    return f"{full[: MAX_DETAIL_CHARS - 1]}…" if len(full) > MAX_DETAIL_CHARS else full


def _standing_suffix(finding: dict[str, Any]) -> str:
    """Mark a finding carried from an earlier patchset, or return empty text."""
    standing = finding.get("standing")
    if not isinstance(standing, dict):
        return ""
    origin = str(standing.get("origin_revision") or "").strip()
    if not origin:
        return " (standing since an earlier patchset)"
    return f" (standing since patchset {origin})"


def _criterion_of(finding: dict[str, Any]) -> str:
    criteria = finding.get("criteria") or []
    if criteria:
        return str(criteria[0])
    return str(finding.get("dimension") or "general")


def render_findings_block(findings: list[dict[str, Any]], *, kind: str) -> str:
    """Render up to MAX_ITEMS findings with criterion, detail, and location.
    Add an overflow note. Empty input returns an empty string. Blocking
    format remains compatible with the adapter's prior output."""
    if not findings:
        return ""
    lines = [_HEADERS.get(kind, "{n} finding(s):").format(n=len(findings))]
    for f in findings[:MAX_ITEMS]:
        loc = f" [{f.get('location')}]" if f.get("location") else ""
        lines.append(f"- ({_criterion_of(f)}) {_detail_of(f)}{_standing_suffix(f)}{loc}")
    omitted = len(findings) - MAX_ITEMS
    if omitted > 0:
        noun = "finding" if omitted == 1 else "findings"
        lines.append(f"{omitted} additional {kind} {noun} omitted from this summary.")
    return "\n".join(lines)


def build_inline_comments(findings: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Build resolved Gerrit comments for already validated finding anchors.
    Advisory comments never leave unresolved threads that block submission."""
    comments: dict[str, list[dict[str, Any]]] = {}
    for f in findings:
        anchor = parse_anchor(f.get("location"))
        if anchor is None or anchor[1] is None:  # pragma: no cover — partition_findings filtered
            continue
        path, line = anchor
        comments.setdefault(path, []).append(
            {
                "line": line,
                "message": f"({_criterion_of(f)}) {_detail_of(f)}{_standing_suffix(f)}",
                "unresolved": False,
            }
        )
    return comments


def _revision_paths(gc: Any, change_id: str, revision: str) -> set[str]:
    """The revision's real file paths, or an empty set if the map cannot be read. Best-effort by
    design: an unreadable map must cost inline comments, never the vote."""
    try:
        files = gc.get_revision_files(change_id, revision)
    except Exception as exc:  # noqa: BLE001 — degrade to body-only, never fail the vote
        logger.warning("finding_publish: revision file map unreadable (%s); body-only", exc)
        return set()
    if not isinstance(files, dict):
        return set()
    return {p for p in files if not p.startswith("/")}


def post_review(
    gc: Any,
    change_id: str,
    revision: str,
    value: int,
    message: str,
    findings: list[dict[str, Any]],
) -> int:
    """Post the vote with optional inline findings and return its HTTP status.
    If inline posting fails, retry message-only with all findings and a
    publication notice. A closed-change 409 or failure without inline
    comments remains terminal."""
    comments: dict[str, list[dict[str, Any]]] = {}
    if findings:
        anchorable, _ = partition_findings(findings, _revision_paths(gc, change_id, revision))
        comments = build_inline_comments(anchorable)

    try:
        return gc.post_vote(change_id, revision, value, message, comments=comments or None)
    except GerritError as exc:
        if not comments or getattr(exc, "status", None) == 409:
            raise
        # Bind out of the except scope: ``exc`` is unbound once the handler exits.
        failure = exc
        logger.warning("finding_publish: inline comments rejected (%s); retrying body-only", exc)

    notice = (
        f"\n\nNote: {sum(len(v) for v in comments.values())} inline comment(s) could not be "
        f"published to this change ({failure}); the findings above are the complete set for this "
        "review, and they are also recorded durably in its code_review artifact."
    )
    return gc.post_vote(change_id, revision, value, f"{message}{notice}", comments=None)
