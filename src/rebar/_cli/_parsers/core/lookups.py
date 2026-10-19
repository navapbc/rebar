"""``rebar resolve`` parser factory (RP-05 S2b).

Prog-bound argparse rendering of the resolution/display arm in
:mod:`rebar._engine_support.lookups`. It takes a positional ticket id and an optional
``--display[=MODE]`` (which replaced the removed ``exists``/``format`` verbs). It does not
accept ``--output`` (it emits a bare resolved string / exit code). The handlers keep
their bespoke ``Usage:`` diagnostics and exit codes. Only the stdlib and
:mod:`rebar._cli._parser` are imported at module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def _base(prog: str, description: str | None = None) -> argparse.ArgumentParser:
    return build_argument_parser(
        prog=prog, description=description, add_help=False, allow_abbrev=False
    )


def build_resolve(*, prog: str) -> argparse.ArgumentParser:
    """``rebar resolve <id_or_alias_or_prefix> [--display[=MODE]]``."""
    parser = _base(
        prog,
        "Resolve an id/alias/prefix to a canonical ticket id (exit 0=found, 1=not found); "
        "--display renders it in a display mode instead.",
    )
    parser.add_argument(
        "--display",
        nargs="?",
        const="",
        metavar="MODE",
        help="render the resolved ticket for display (default: the configured display mode)",
    )
    parser.add_argument("ticket_id", nargs="?", help="the id, alias, or prefix to resolve")
    return parser
