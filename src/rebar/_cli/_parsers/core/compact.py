"""``rebar compact`` / ``compact-all`` parser factories (RP-05 S2b).

Prog-bound argparse renderings of the two compaction arms in
:mod:`rebar._commands.compact`. ``compact`` takes a positional ticket id plus
``--threshold=`` / ``--horizon=`` / ``--skip-sync`` / ``--no-commit``;
``compact-all`` is store-wide (no id) with ``--dry-run`` / ``--limit=`` /
``--no-commit`` / ``--include-archived``. Neither accepts ``--output``. The
handlers keep their bespoke ``Usage:`` diagnostics and exit codes. Only the stdlib
and :mod:`rebar._cli._parser` are imported at module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def build_compact(*, prog: str) -> argparse.ArgumentParser:
    """``rebar compact <id> [--threshold=N] [--horizon=NS] [--skip-sync] [--no-commit]``."""
    parser = build_argument_parser(
        prog=prog,
        description="Compact a ticket's event log.",
        add_help=False,
        allow_abbrev=False,
    )
    parser.add_argument("--threshold", metavar="N", help="compact only above N events")
    parser.add_argument("--horizon", metavar="NS", help="retain events newer than NS nanoseconds")
    parser.add_argument("--skip-sync", action="store_true", help="skip the reconverge")
    parser.add_argument(
        "--no-commit", action="store_true", help="stage the snapshot without committing"
    )
    parser.add_argument("ticket_id", nargs="?", help="the ticket to compact")
    return parser


def build_compact_all(*, prog: str) -> argparse.ArgumentParser:
    """``rebar compact-all [--dry-run] [--limit=N] [--no-commit] [--include-archived]``."""
    parser = build_argument_parser(
        prog=prog,
        description="Compact all eligible tickets.",
        add_help=False,
        allow_abbrev=False,
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would compact without writing"
    )
    parser.add_argument("--limit", metavar="N", help="compact at most N tickets")
    parser.add_argument(
        "--no-commit", action="store_true", help="stage the snapshots without committing"
    )
    parser.add_argument(
        "--include-archived", action="store_true", help="also compact archived tickets"
    )
    return parser
