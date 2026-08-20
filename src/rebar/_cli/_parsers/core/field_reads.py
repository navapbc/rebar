"""``rebar get-file-impact`` / ``get-verify-commands`` parser factories (RP-05 S2b).

Prog-bound argparse renderings of the two field-read arms in
:mod:`rebar._engine_support.field_reads`. Both take a single positional ticket id;
``get-verify-commands`` additionally honours the ``report`` ``--output`` profile
(text default, json allowed). The handlers keep their bespoke ``Usage:`` /
``Error: ticket_id must be non-empty`` diagnostics and exit codes. Only the stdlib
and :mod:`rebar._cli._parser` are imported at module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def build_get_file_impact(*, prog: str) -> argparse.ArgumentParser:
    """``rebar get-file-impact <ticket_id>``."""
    parser = build_argument_parser(prog=prog, add_help=False, allow_abbrev=False)
    parser.add_argument("ticket_id", nargs="?")
    return parser


def build_get_verify_commands(*, prog: str) -> argparse.ArgumentParser:
    """``rebar get-verify-commands <ticket_id> [--output json]``."""
    parser = build_argument_parser(prog=prog, add_help=False, allow_abbrev=False)
    parser.add_argument("--output", "-o", choices=("text", "json"), default="text")
    parser.add_argument("ticket_id", nargs="?")
    return parser
