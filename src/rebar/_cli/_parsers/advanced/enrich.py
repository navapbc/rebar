"""``rebar enrich`` parser factory (RP-05 S2c, census).

A prog-bound argparse rendering of the ``rebar enrich`` grammar owned today by the
hand-rolled dispatch in :func:`rebar.llm.enrich_drain.cmd_enrich` (``status`` |
``[--drain] [--once]``). Registered for registry census + the AC3
import-isolation probe; the runtime handler keeps its bespoke parsing. Only the
stdlib and :mod:`rebar._cli._parser` are imported at module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def build(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar enrich`` parser bound to ``prog``."""
    parser = build_argument_parser(
        prog=prog,
        description="Inspect or drain the ticket enrichment queue.",
        formatter_class=argparse.HelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=("status",),
        help="'status' prints the queue buckets as JSON. Omit to drain",
    )
    parser.add_argument("--drain", action="store_true", help="bounded drain of the overlap queue")
    parser.add_argument("--once", action="store_true", help="process a single queue entry")
    return parser
