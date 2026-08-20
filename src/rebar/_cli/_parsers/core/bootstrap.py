"""``rebar init`` / ``scratch`` parser factories (RP-05 S2b).

Prog-bound argparse renderings of the two bootstrap arms in
:mod:`rebar._commands.init` and :mod:`rebar._commands.scratch`. ``init`` accepts
only the ``--silent`` / ``--force-new-store`` toggles (no store required — it
creates one); ``scratch`` is a ``<verb> [args...]`` dispatcher (``set`` / ``get`` /
``clear``). Neither accepts ``--output``. The handlers keep their bespoke
``Usage:`` / ``unknown init option`` / ``unknown_verb`` diagnostics and exit codes.
Only the stdlib and :mod:`rebar._cli._parser` are imported at module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def build_init(*, prog: str) -> argparse.ArgumentParser:
    """``rebar init [--silent] [--force-new-store]``."""
    parser = build_argument_parser(prog=prog, add_help=False, allow_abbrev=False)
    parser.add_argument("--silent", action="store_true")
    parser.add_argument("--force-new-store", action="store_true")
    return parser


def build_scratch(*, prog: str) -> argparse.ArgumentParser:
    """``rebar scratch <verb> [args...]`` (verb ∈ set/get/clear)."""
    parser = build_argument_parser(prog=prog, add_help=False, allow_abbrev=False)
    parser.add_argument("verb", nargs="?")
    parser.add_argument("args", nargs="*")
    return parser
