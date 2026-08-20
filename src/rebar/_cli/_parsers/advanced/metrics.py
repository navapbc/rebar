"""``rebar metrics`` parser factory (RP-05 S2c, census).

A prog-bound argparse rendering of the ``rebar metrics`` grammar owned today by
the hand-rolled flag walk in :func:`rebar._commands.metrics.metrics_cli`
(``[--since <date>] [--until <date>] [--output json|text]``). Registered for
registry census + the AC3 import-isolation probe; the runtime handler keeps its
bespoke parsing. Only the stdlib and :mod:`rebar._cli._parser` are imported at
module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def build(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar metrics`` parser bound to ``prog``."""
    parser = build_argument_parser(
        prog=prog, formatter_class=argparse.HelpFormatter, allow_abbrev=False
    )
    parser.add_argument("--since", metavar="DATE", help="inclusive start date (default: 30d ago)")
    parser.add_argument("--until", metavar="DATE", help="inclusive end date (default: today)")
    parser.add_argument(
        "--output", choices=("json", "text"), default="json", help="output format (default: json)"
    )
    return parser
