"""``rebar grounding-info`` parser factory (RP-05 S2b).

Prog-bound argparse rendering of the static code-grounding oracle read served by
``rebar._cli._grounding_info``. It is repo-independent, takes no positionals, and
honours the ``report`` ``--output`` profile (human summary by default, the
``grounding_info`` schema under ``--output json``). The handler keeps its bespoke
``Usage:`` diagnostic and exit codes. Only the stdlib and
:mod:`rebar._cli._parser` are imported at module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def build(*, prog: str) -> argparse.ArgumentParser:
    """``rebar grounding-info [--output json]``."""
    parser = build_argument_parser(prog=prog, add_help=False, allow_abbrev=False)
    parser.add_argument("--output", "-o", choices=("text", "json"), default="text")
    return parser
