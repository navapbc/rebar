"""``rebar store compact`` parser factories (RP-05 S2b).

Prog-bound argparse renderings of the two compaction arms in
:mod:`rebar._commands.compact`. The one-ticket arm (:func:`build_compact_one`) takes a
positional ticket id plus ``--threshold=`` / ``--horizon=`` / ``--skip-sync`` /
``--no-commit``; the store-wide sweep (:func:`build_compact_all`, selected by ``--all``)
takes ``--dry-run`` / ``--limit=`` / ``--no-commit`` / ``--include-archived``. Neither
accepts ``--output``. :func:`build_compact` is the help-facing union that
``rebar store compact --help`` renders; the handlers parse with the two arm factories and
keep their bespoke ``Usage:`` diagnostics and exit codes. Only the stdlib and
:mod:`rebar._cli._parser` are imported at module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def _add_one_ticket_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--threshold", metavar="N", help="compact only above N events")
    parser.add_argument("--horizon", metavar="NS", help="retain events newer than NS nanoseconds")
    parser.add_argument("--skip-sync", action="store_true", help="skip the reconverge")


def _add_sweep_flags(parser: argparse.ArgumentParser, *, marker: str = "") -> None:
    parser.add_argument(
        "--dry-run", action="store_true", help=f"{marker}report what would compact without writing"
    )
    parser.add_argument("--limit", metavar="N", help=f"{marker}compact at most N tickets")
    parser.add_argument(
        "--include-archived", action="store_true", help=f"{marker}also compact archived tickets"
    )


def build_compact_one(*, prog: str) -> argparse.ArgumentParser:
    """``rebar store compact <id> [--threshold=N] [--horizon=NS] [--skip-sync] [--no-commit]``."""
    parser = build_argument_parser(
        prog=prog,
        description="Compact a ticket's event log.",
        add_help=False,
        allow_abbrev=False,
    )
    _add_one_ticket_flags(parser)
    parser.add_argument(
        "--no-commit", action="store_true", help="stage the snapshot without committing"
    )
    parser.add_argument("ticket_id", nargs="?", help="the ticket to compact")
    return parser


def build_compact_all(*, prog: str) -> argparse.ArgumentParser:
    """``rebar store compact --all [--dry-run] [--limit=N] [--no-commit] [--include-archived]``."""
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


def build_compact(*, prog: str) -> argparse.ArgumentParser:
    """Help-facing ``rebar store compact``: the one-ticket arm, or the sweep under ``--all``."""
    parser = build_argument_parser(
        prog=prog,
        description="Compact one ticket's event log, or every eligible ticket with --all.",
        add_help=False,
        allow_abbrev=False,
    )
    parser.add_argument(
        "--all", action="store_true", help="compact every eligible ticket (the store-wide sweep)"
    )
    _add_one_ticket_flags(parser)
    parser.add_argument(
        "--no-commit", action="store_true", help="stage the snapshot(s) without committing"
    )
    _add_sweep_flags(parser, marker="(with --all) ")
    parser.add_argument("ticket_id", nargs="?", help="the ticket to compact (omit with --all)")
    return parser
