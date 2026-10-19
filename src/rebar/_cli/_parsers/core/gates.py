"""``rebar`` gate-command parser factories (RP-05 S2b).

Prog-bound argparse renderings of the two gate arms in
:mod:`rebar._engine_support.gates` (``check`` and ``summary``). ``check`` merges the removed
``check-ac``/``clarity-check``/``quality-check`` verbs behind ``--ac``/``--clarity``/(default
dispatch readiness); ``summary`` accepts several ticket ids. Both honour the ``report``
``--output`` profile; ``check --clarity`` also takes ``--stdin`` and ``--config <path>``.
The handlers keep their bespoke diagnostics and exit codes. Only the stdlib and
:mod:`rebar._cli._parser` are imported at module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def _base(prog: str, description: str | None = None) -> argparse.ArgumentParser:
    return build_argument_parser(
        prog=prog, description=description, add_help=False, allow_abbrev=False
    )


def build_summary(*, prog: str) -> argparse.ArgumentParser:
    """``rebar summary <id> [<id> ...] [--output json]``."""
    parser = _base(prog, "One-line ticket summary with blocking status for one or more IDs.")
    parser.add_argument(
        "--output", "-o", choices=("text", "json"), default="text", help="output format"
    )
    parser.add_argument("ticket_id", nargs="*", help="one or more tickets to summarize")
    return parser


def build_check(*, prog: str) -> argparse.ArgumentParser:
    """``rebar check <id> [--ac | --clarity] [--output json]``; ``--clarity`` also takes
    ``--stdin`` / ``--config <path>``. With no mode flag it checks dispatch readiness."""
    parser = _base(
        prog,
        "Check one ticket: dispatch readiness by default, --ac for the Acceptance Criteria "
        "block, --clarity for the clarity score.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--ac", action="store_true", help="check the Acceptance Criteria block")
    mode.add_argument("--clarity", action="store_true", help="score clarity (exit 0=pass, 1=fail)")
    parser.add_argument(
        "--output", "-o", choices=("text", "json"), default="text", help="output format"
    )
    parser.add_argument(
        "--stdin", action="store_true", help="with --clarity: read the ticket text from stdin"
    )
    parser.add_argument("--config", help="with --clarity: path to a clarity config override")
    parser.add_argument("ticket_id", nargs="?", help="the ticket to check")
    return parser
