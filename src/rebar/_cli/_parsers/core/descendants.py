"""``rebar list-descendants`` parser factory (RP-05 S2b).

Prog-bound argparse rendering of the BFS descendants read in
:mod:`rebar._engine_support.descendants`. It takes a single positional root ticket
id and emits a fixed JSON bucket shape (no ``--output``). The handler keeps its
bespoke ``Usage:`` diagnostic and graceful empty-arrays behaviour. Only the stdlib
and :mod:`rebar._cli._parser` are imported at module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def build(*, prog: str) -> argparse.ArgumentParser:
    """``rebar list-descendants <ticket_id>``."""
    parser = build_argument_parser(
        prog=prog,
        description="BFS walk from a root ticket, bucketed by type (JSON).",
        add_help=False,
        allow_abbrev=False,
    )
    parser.add_argument("ticket_id", nargs="?", help="the root ticket to walk from")
    return parser
