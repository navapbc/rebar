"""``rebar exists`` / ``resolve`` / ``format`` parser factories (RP-05 S2b).

Prog-bound argparse renderings of the three resolution/display arms in
:mod:`rebar._engine_support.lookups`. All take a positional ticket id; ``format``
also accepts an optional trailing display ``mode`` positional. None accepts
``--output`` (they emit a bare resolved string / exit code). The handlers keep
their bespoke ``Usage:`` diagnostics and exit codes. Only the stdlib and
:mod:`rebar._cli._parser` are imported at module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def _base(prog: str) -> argparse.ArgumentParser:
    return build_argument_parser(prog=prog, add_help=False, allow_abbrev=False)


def build_exists(*, prog: str) -> argparse.ArgumentParser:
    """``rebar exists <ticket_id>``."""
    parser = _base(prog)
    parser.add_argument("ticket_id", nargs="?")
    return parser


def build_resolve(*, prog: str) -> argparse.ArgumentParser:
    """``rebar resolve <id_or_alias_or_prefix>``."""
    parser = _base(prog)
    parser.add_argument("ticket_id", nargs="?")
    return parser


def build_format(*, prog: str) -> argparse.ArgumentParser:
    """``rebar format <ticket_id> [mode]``."""
    parser = _base(prog)
    parser.add_argument("ticket_id", nargs="?")
    parser.add_argument("mode", nargs="?")
    return parser
