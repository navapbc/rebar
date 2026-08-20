"""``rebar config`` parser factories (RP-05 S2c).

Prog-bound factories reproducing the two parsers in
:func:`rebar._commands.show_config.config_cli`: the top-level ``rebar config``
parser and its ``validate`` pseudo-subcommand parser (dispatched by the handler on
``argv[0] == "validate"``, not via argparse subparsers). Both keep the stdlib
default ``HelpFormatter`` (width preserved). Only the stdlib and
:mod:`rebar._cli._parser` are imported at module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def build(*, prog: str) -> argparse.ArgumentParser:
    """Build the top-level ``rebar config`` parser bound to ``prog``."""
    parser = build_argument_parser(
        prog=prog,
        description="Show the resolved rebar configuration and the precedence layer "
        "(cli > env > project > user > default) each value came from.",
        formatter_class=argparse.HelpFormatter,
    )
    parser.add_argument("--output", "-o", choices=["text", "json"], default="text")
    parser.add_argument(
        "--root", default=None, help="repo root for project-config discovery (default: auto)"
    )
    return parser


def build_validate(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar config validate`` subcommand parser bound to ``prog``."""
    parser = build_argument_parser(
        prog=prog,
        description="Scan every config layer for invalid known typed values and "
        "REMOVED (tombstoned) inputs. Exits non-zero on any typed failure or "
        "load-bearing removed input.",
        formatter_class=argparse.HelpFormatter,
    )
    parser.add_argument(
        "--root", default=None, help="repo root for config discovery (default: auto)"
    )
    return parser
