"""``rebar import`` / ``export`` parser factories (RP-05 S2b).

Prog-bound argparse renderings of the two NDJSON I/O arms in
:mod:`rebar._io._cli`. ``export`` carries the filter/output surface (``-o/--out``
plus status/type/parent selectors and the strip/include toggles); ``import`` takes
an optional ``FILE`` positional (stdin when omitted) and ``--dry-run``. The
handlers keep their bespoke ``Error: unknown option`` diagnostics and exit codes.
Only the stdlib and :mod:`rebar._cli._parser` are imported at module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def build_export(*, prog: str) -> argparse.ArgumentParser:
    """``rebar export [-o FILE] [--status S] [--type T] [--parent ID] [toggles]``."""
    parser = build_argument_parser(
        prog=prog,
        description="Export the store as NDJSON (one ticket per line).",
        add_help=False,
        allow_abbrev=False,
    )
    parser.add_argument("--out", "-o", dest="out", help="write to FILE instead of stdout")
    parser.add_argument("--status", help="filter by status")
    parser.add_argument("--type", dest="ticket_type", help="filter by ticket type")
    parser.add_argument("--parent", help="filter to descendants of <id>")
    parser.add_argument(
        "--strip-external",
        "--no-jira",
        dest="strip_external",
        action="store_true",
        help="strip external (Jira) bindings from the export",
    )
    parser.add_argument(
        "--include-session-logs", action="store_true", help="include session_log tickets"
    )
    parser.add_argument("--exclude-archived", action="store_true", help="exclude archived tickets")
    parser.add_argument("--include-deleted", action="store_true", help="include deleted tickets")
    return parser


def build_import(*, prog: str) -> argparse.ArgumentParser:
    """``rebar import [FILE] [--dry-run]``   (reads stdin if FILE omitted)."""
    parser = build_argument_parser(
        prog=prog,
        description="Import tickets from export NDJSON for a clean rebar-to-rebar migration.",
        epilog=(
            "Ticket creation, parent assignment, file impact, verification commands, and "
            "comments are committed in batches of up to 256 events. Links and status "
            "changes are committed one event at a time. Rebar defers the push until import "
            "work finishes successfully. Each run scans stored source_id values and skips "
            "matching records, so a serial rerun does not duplicate tickets created by an "
            "earlier run. The import does not provide whole-file atomicity. A crash between "
            "passes can leave incomplete tickets. A serial rerun skips those tickets based "
            "on source_id and does not complete the missing events automatically. Run "
            "imports serially. Concurrent imports can scan before either run records a "
            "source_id and can create duplicate tickets."
        ),
        add_help=False,
        allow_abbrev=False,
    )
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("file", nargs="?", help="NDJSON file to import (stdin if omitted)")
    return parser
