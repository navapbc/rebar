"""``rebar delete`` parser factory (RP-05 S2b).

Prog-bound argparse rendering of the destructive soft-delete arm in
:mod:`rebar._commands.delete`. It takes a positional ticket id and REQUIRES the
``--user-approved`` guard flag; it honours the ``report`` ``--output`` profile. The
handler keeps its bespoke ``Usage:`` / ``requires --user-approved`` diagnostics and
exit codes. Only the stdlib and :mod:`rebar._cli._parser` are imported at module
top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def build(*, prog: str) -> argparse.ArgumentParser:
    """``rebar delete <ticket_id> --user-approved [--output json]``."""
    parser = build_argument_parser(prog=prog, add_help=False, allow_abbrev=False)
    parser.add_argument("--output", "-o", choices=("text", "json"), default="text")
    parser.add_argument("--user-approved", action="store_true")
    parser.add_argument("ticket_id", nargs="?")
    return parser
