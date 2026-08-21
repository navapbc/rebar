"""``rebar`` read-command parser factories (RP-05 S2b).

Prog-bound argparse renderings of the read arms owned by the hand-rolled dispatch
in :mod:`rebar._engine_support.reads_cli` (plus ``validate``, the repo-wide health
read in :mod:`rebar._engine_support.validate`). Each models the ACCEPTED grammar —
the ``--output`` profile spelling, the family-specific filters, the positional
ticket id(s) — while the runtime handlers keep their bespoke unknown-option
diagnostics (``Error: unknown option``, next-batch's ``Unknown flag``, validate's
``Unknown option``) and exit codes. Only the stdlib and
:mod:`rebar._cli._parser` are imported at module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser

# The read arms hand-handle ``--help``/``-h`` (print usage, exit 0) rather than
# using argparse's auto-help action, so every factory is built with
# ``add_help=False`` and ``allow_abbrev=False`` (no prefix matching — a truncated
# flag stays an unknown token, matching the loops' exact-token comparison).
_SORT_METAVAR = "<priority|created|updated|id|status>"


def _base(prog: str, description: str | None = None) -> argparse.ArgumentParser:
    return build_argument_parser(
        prog=prog, description=description, add_help=False, allow_abbrev=False
    )


def build_show(*, prog: str) -> argparse.ArgumentParser:
    """``rebar show [--output llm] [--include-scratch] <ticket_id>...``."""
    parser = _base(prog, "Show ticket details.")
    parser.add_argument(
        "--output", "-o", choices=("json", "llm"), default="json", help="output format"
    )
    parser.add_argument(
        "--include-scratch", action="store_true", help="include per-ticket scratch values"
    )
    # Reconciler-only, intentionally undocumented (kept out of usage).
    parser.add_argument("--include-provenance", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("ticket_id", nargs="*", help="one or more ticket ids/aliases")
    return parser


def build_list(*, prog: str) -> argparse.ArgumentParser:
    """``rebar list`` — the full filter surface (all optional)."""
    parser = _base(prog, "List all tickets as JSON.")
    parser.add_argument(
        "--output", "-o", choices=("json", "llm"), default="json", help="output format"
    )
    parser.add_argument("--include-archived", action="store_true", help="include archived tickets")
    parser.add_argument("--exclude-deleted", action="store_true", help="exclude deleted tickets")
    parser.add_argument("--full", action="store_true", help="emit full ticket records")
    parser.add_argument(
        "--with-children-count", action="store_true", help="annotate each ticket's child count"
    )
    parser.add_argument(
        "--unblocked", action="store_true", help="only tickets with no open blockers"
    )
    parser.add_argument("--blocked", action="store_true", help="only tickets with open blockers")
    parser.add_argument("--type", help="filter by ticket type (comma-separated for OR)")
    parser.add_argument("--status", help="filter by status (comma-separated for OR)")
    parser.add_argument("--priority", help="filter by priority 0-4 (comma-separated for OR)")
    parser.add_argument("--parent", help="filter to direct children of <id>")
    parser.add_argument("--has-tag", help="filter to tickets having <tag> (comma-separated for OR)")
    parser.add_argument(
        "--without-tag", help="exclude tickets having any of <tag> (comma-separated)"
    )
    parser.add_argument("--min-children", help="only tickets with at least N children")
    parser.add_argument(
        "--sort", metavar=_SORT_METAVAR, help="order by key ('-' prefix = descending)"
    )
    return parser


def build_next_batch(*, prog: str) -> argparse.ArgumentParser:
    """``rebar next-batch <epic-id> [--limit=N|unlimited] [--output json]``."""
    parser = _base(prog, "Select next parallel agent batch for an epic.")
    parser.add_argument(
        "--output", "-o", choices=("json", "text", "report"), default="text", help="output format"
    )
    parser.add_argument("--limit", metavar="N", help="cap the batch size (or 'unlimited')")
    parser.add_argument("epic", nargs="?", help="the epic to select a batch for")
    return parser


def build_deps(*, prog: str) -> argparse.ArgumentParser:
    """``rebar deps <ticket_id> [--include-archived]`` (no ``--output``)."""
    parser = _base(prog, "Show dependency graph for a ticket.")
    parser.add_argument(
        "--include-archived", action="store_true", help="include archived tickets in the graph"
    )
    parser.add_argument("ticket_id", nargs="?", help="the ticket to graph")
    return parser


def build_ready(*, prog: str) -> argparse.ArgumentParser:
    """``rebar ready [--output json|llm] [--epic <id>] [--sort=...]``."""
    parser = _base(prog, "List tickets ready to work (all blockers closed).")
    parser.add_argument(
        "--output", "-o", choices=("text", "llm", "json"), default="text", help="output format"
    )
    parser.add_argument("--epic", help="restrict to descendants of <id>")
    parser.add_argument(
        "--sort", metavar=_SORT_METAVAR, help="order by key ('-' prefix = descending)"
    )
    return parser


def build_search(*, prog: str) -> argparse.ArgumentParser:
    """``rebar search <query> [--output json|llm] [--full] [filters...]``."""
    parser = _base(prog, "Full-text search over titles/descriptions/comments/tags.")
    parser.add_argument(
        "--output", "-o", choices=("json", "llm"), default="json", help="output format"
    )
    parser.add_argument("--full", action="store_true", help="emit full ticket records")
    parser.add_argument("--include-archived", action="store_true", help="include archived tickets")
    parser.add_argument("--status", help="filter by status (comma-separated for OR)")
    parser.add_argument("--type", help="filter by ticket type (comma-separated for OR)")
    parser.add_argument("--has-tag", help="filter to tickets having <tag> (comma-separated for OR)")
    parser.add_argument(
        "--sort", metavar=_SORT_METAVAR, help="order by key ('-' prefix = descending)"
    )
    parser.add_argument("query", nargs="?", help="the search query")
    return parser


def build_session_logs(*, prog: str) -> argparse.ArgumentParser:
    """``rebar session-logs [--output json|llm] [--limit=<n>]``."""
    parser = _base(prog, "List the newest session_log tickets, newest first.")
    parser.add_argument(
        "--output", "-o", choices=("json", "llm"), default="json", help="output format"
    )
    parser.add_argument("--limit", metavar="N", help="cap the number of logs returned")
    return parser


def build_validate(*, prog: str) -> argparse.ArgumentParser:
    """``rebar validate [--quick] [--full] [--verbose] [--output json] [--terse]``."""
    parser = _base(prog, "Repo-wide tracker health check (scores the whole store 1-5).")
    parser.add_argument(
        "--output", "-o", choices=("text", "json"), default="text", help="output format"
    )
    parser.add_argument("--quick", action="store_true", help="run only the fast checks")
    parser.add_argument("--full", action="store_true", help="run the exhaustive checks")
    parser.add_argument("--verbose", "-v", action="store_true", help="emit per-check detail")
    parser.add_argument("--terse", action="store_true", help="emit a single score line")
    return parser
