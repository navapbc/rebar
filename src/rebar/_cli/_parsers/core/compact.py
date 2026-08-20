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
    parser = build_argument_parser(prog=prog, add_help=False, allow_abbrev=False)
    parser.add_argument("--threshold", metavar="N")
    parser.add_argument("--horizon", metavar="NS")
    parser.add_argument("--skip-sync", action="store_true")
    parser.add_argument("--no-commit", action="store_true")
    parser.add_argument("ticket_id", nargs="?")
    return parser


def build_compact_all(*, prog: str) -> argparse.ArgumentParser:
    """``rebar compact-all [--dry-run] [--limit=N] [--no-commit] [--include-archived]``."""
    parser = build_argument_parser(prog=prog, add_help=False, allow_abbrev=False)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", metavar="N")
    parser.add_argument("--no-commit", action="store_true")
    parser.add_argument("--include-archived", action="store_true")
    return parser
