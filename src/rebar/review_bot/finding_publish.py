"""Publishing review findings ONTO the Gerrit change (bug lacquer-grotesque-urson).

Before this module the bot published advisory findings as a bare COUNT and nothing else:
``_summarize``'s PASS branch rendered ``len(verdict["advisory"])`` and never itemised the
findings, its ``"finding"`` branch itemised blocking findings only, and ``_translate_findings``
discarded each finding's ``location`` so no anchor ever reached the Gerrit layer. The text was
retrievable NOWHERE on the change — not in the message, not as an inline comment, and not in the
patchset-level robot comment either, because this Gerrit answers ``/robotcomments`` with
"robot comments unsupported". A reviewer could not judge which advisory criteria deserved
promotion to blocking.

Two surfaces, deliberately overlapping:

* **The message body is the guarantee.** Every finding handed to :func:`post_review` is
  enumerated in the message when it is not inlined, and ALL of them are enumerated when inline
  publication fails. The body is the only surface this server is proven to render.
* **Inline comments are the bonus.** A finding whose ``location`` anchors to a path that really
  is in the revision also lands as a per-line comment, so it shows up where the reviewer reads
  the diff. Anchors are validated against the revision's file map first: a comment on a path
  Gerrit does not know is a 400 that would take the whole vote down with it.

Both failure directions degrade toward "visible", never toward "silent" — an unreadable file map
means nothing is inlined (body only), and a rejected comment-bearing POST is retried message-only
with an explicit notice naming the durable ``code_review`` artifact as the full record.

This module also OWNS the enumeration rendering that ``adapter._summarize`` used to inline for
blocking findings; both adapter branches now call :func:`render_findings_block`, so the wording,
the truncation and the overflow note are defined once.
"""

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

#: Per-finding detail budget in the message body, and the number of findings itemised before the
#: overflow note takes over. Both are the limits ``adapter._summarize`` applied to blocking
#: findings before this module existed, promoted to named constants so the two branches share them.
MAX_DETAIL_CHARS = 240
MAX_ITEMS = 10

#: Headline for each enumerated block, by finding kind.
_HEADERS = {
    "blocking": "rebar code review found {n} blocking issue(s):",
    "advisory": "{n} advisory finding(s) (non-blocking):",
}


def parse_anchor(location: Any) -> tuple[str, int | None] | None:
    """Split a free-text finding ``location`` into ``(path, line)``, or ``None`` when there is no
    usable path.

    Peer authority: :func:`rebar.llm.code_review.shim._parse_location`, which reads the SAME
    field for the local ``review_result`` citation shape. The rule is deliberately identical —
    ``rpartition(":")``, take the first number of a ``12-20`` / ``12,4`` tail, and fall back to
    "the whole string is the path" when that tail is not a number — and
    ``test_parse_anchor_agrees_with_the_review_result_location_parser`` pins them together so they
    cannot drift. The parsers are not shared outright because this module must stay importable
    without the heavy ``[agents]`` extra that ``shim`` pulls in.
    """
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
    """Split ``findings`` into ``(anchorable, anchorless)``.

    A finding is anchorable only when its location parses to a path that is a REAL key of the
    revision's file map AND carries a line number. Anything else — no location, a prose location,
    a hallucinated path, a path with no line — is anchorless and belongs in the message body.
    Validating against the live file map is what keeps a bad path from turning the vote POST into
    a 400."""
    anchorable: list[dict[str, Any]] = []
    anchorless: list[dict[str, Any]] = []
    for f in findings:
        anchor = parse_anchor(f.get("location"))
        if anchor is not None and anchor[1] is not None and anchor[0] in revision_paths:
            anchorable.append(f)
        else:
            anchorless.append(f)
    return anchorable, anchorless


# Two finding shapes reach this module and BOTH must render. The adapter's message body is built
# from the RAW kernel verdict (``{finding, criteria, location}``); the voter hands post_review the
# TRANSLATED shape the receiver logs (``{severity, dimension, detail, location}``). Reading only
# the raw keys silently produced empty inline comments — the accessors below accept either.
def _detail_of(finding: dict[str, Any]) -> str:
    raw = finding.get("finding") or finding.get("detail") or ""
    full = str(raw).strip().replace("\n", " ")
    return f"{full[: MAX_DETAIL_CHARS - 1]}…" if len(full) > MAX_DETAIL_CHARS else full


def _standing_suffix(finding: dict[str, Any]) -> str:
    """`` (standing since patchset <k>)`` for a finding carried forward from an earlier patchset
    (story nitro-zombie-mealworm), or ``""`` for an ordinary one. A reader must be able to tell a
    finding this run's finders raised from one the gate is re-raising on their behalf — otherwise a
    carried finding looks brand new on every patchset."""
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
    """Enumerate ``findings`` as message-body text: a header, up to :data:`MAX_ITEMS` bullets of
    ``- (criterion) detail [location]``, and an overflow note for the remainder. Returns ``""``
    for an empty list so callers can append unconditionally.

    For ``kind="blocking"`` the output is byte-identical to the block ``adapter._summarize``
    produced before this module existed — that equivalence is pinned by
    ``test_blocking_enumeration_keeps_its_pre_fix_shape``."""
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
    """Gerrit ``CommentInput`` map — ``{path: [{line, message, unresolved}]}`` — for findings
    already proven anchorable by :func:`partition_findings`.

    ``unresolved`` is always ``False``: submission requires no unresolved comments, so an
    advisory (by definition non-blocking) must never be able to hold a change back. That is what
    keeps this additive surface out of the vote's semantics."""
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
    """Cast the vote with ``findings`` published as inline comments where they anchor, and return
    the HTTP status.

    ``message`` already enumerates every finding (the adapter's guarantee), so this call adds
    only the inline layer on top. If the comment-bearing POST is rejected, the vote is retried
    message-only with that body preserved verbatim plus a notice that inline publication failed
    and where the full set lives — the vote still lands and no finding text is lost. A ``409``
    (change closed) is terminal and re-raised untouched so the voter's existing skip path handles
    it, as is a failure of a POST that carried no comments in the first place."""
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
