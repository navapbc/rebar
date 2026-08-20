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
    parser = build_argument_parser(prog=prog, add_help=False, allow_abbrev=False)
    parser.add_argument("--out", "-o", dest="out")
    parser.add_argument("--status")
    parser.add_argument("--type", dest="ticket_type")
    parser.add_argument("--parent")
    parser.add_argument("--strip-external", "--no-jira", dest="strip_external", action="store_true")
    parser.add_argument("--include-session-logs", action="store_true")
    parser.add_argument("--exclude-archived", action="store_true")
    parser.add_argument("--include-deleted", action="store_true")
    return parser


def build_import(*, prog: str) -> argparse.ArgumentParser:
    """``rebar import [FILE] [--dry-run]``   (reads stdin if FILE omitted)."""
    parser = build_argument_parser(prog=prog, add_help=False, allow_abbrev=False)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("file", nargs="?")
    return parser
