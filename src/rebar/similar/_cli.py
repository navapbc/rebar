"""Thin CLI adapter for the explicit similar-ticket vector build.

The build is opt-in and costs provider calls, so it gets a command of its own rather
than a flag on a read. Nothing on the ticket-creation path reaches this module.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from rebar import config
from rebar._mcp_errors import js_safe_dumps

#: The counters :func:`rebar.similar.embedding._new_stats` reports, in display order.
_COUNTERS = ("vectored", "skipped", "empties", "digest_unparsed", "digest_failed")


def _render_text(report: dict[str, Any]) -> str:
    """One line of status, then one line per counter."""
    lines = [f"similar-ticket vector build: {report['status']}"]
    reason = report.get("reason")
    if reason:
        lines.append(f"  reason: {reason}")
    for name in _COUNTERS:
        lines.append(f"  {name}: {report[name]}")
    if "remaining" in report:
        lines.append(f"  remaining: {report['remaining']}")
    return "\n".join(lines) + "\n"


def _unconfigured() -> dict[str, Any]:
    """The report for an installation that has not opted in.

    Detected HERE rather than from :func:`~rebar.similar.embedding.build_vectors`'
    return, because the build reports an absent configuration and a failing provider
    with the same ``unavailable`` status. Those two deserve opposite exit codes -- not
    opting in is not an error, a provider that will not answer is -- and telling them
    apart by matching on reason text would make the exit code hostage to the wording.
    Checking first also makes the guarantee literal: with no model configured the
    build is never entered, so no provider call can occur.
    """
    return {
        **dict.fromkeys(_COUNTERS, 0),
        "status": "unavailable",
        "reason": "no [llm] embedding_model is configured; the lexical tier serves retrieval",
    }


def build_vectors_cli(argv: list[str], *, repo_root: str | None = None) -> int:
    """Run ``rebar store build-vectors``.

    Exit 0 covers every outcome the operator can act on by running it again: a
    complete corpus, a partial one, and an installation that has not configured an
    embedding model. Exit 1 is reserved for a build that could not proceed -- an
    unreadable index or a provider that failed -- where re-running changes nothing
    until the cause is addressed.
    """
    from rebar._cli._parser import ParseError, render_parse_error
    from rebar._cli._parsers.advanced.similar_build import build

    try:
        args = build(prog="rebar store build-vectors").parse_args(argv)
    except ParseError as exc:
        return render_parse_error(exc)

    if args.limit is not None and args.limit < 1:
        sys.stderr.write("Error: --limit must be 1 or greater\n")
        return 2

    from .embedding import build_vectors, embedding_model_name

    if not embedding_model_name():
        report, failed = _unconfigured(), False
    else:
        root = Path(config.repo_root(repo_root) if repo_root is not None else config.repo_root())
        report = build_vectors(str(config.tracker_dir(root)), limit=args.limit)
        # Every `unavailable` reaching here is a real failure: the one benign cause was
        # ruled out above, so this never inspects the reason text to decide.
        failed = report["status"] == "unavailable"

    if args.output == "json":
        sys.stdout.write(js_safe_dumps(report, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(_render_text(report))

    if failed:
        sys.stderr.write(f"Error: {report.get('reason', 'the vector build could not run')}\n")
        return 1
    return 0
