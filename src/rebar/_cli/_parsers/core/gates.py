"""``rebar`` gate-command parser factories (RP-05 S2b).

Prog-bound argparse renderings of the four gate arms in
:mod:`rebar._engine_support.gates` (``check-ac``, ``clarity-check``,
``quality-check``, ``summary``). ``check-ac``/``quality-check``/``summary`` honour
the ``report`` ``--output`` profile and take ticket-id positional(s); ``summary``
accepts several. ``clarity-check`` is the odd one — a ticket id XOR ``--stdin``,
plus ``--config <path>`` — and keeps its distinct ``ERROR: unknown flag`` wording.
The handlers keep their bespoke diagnostics and exit codes. Only the stdlib and
:mod:`rebar._cli._parser` are imported at module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def _base(prog: str) -> argparse.ArgumentParser:
    return build_argument_parser(prog=prog, add_help=False, allow_abbrev=False)


def build_check_ac(*, prog: str) -> argparse.ArgumentParser:
    """``rebar check-ac <id> [--output json]``."""
    parser = _base(prog)
    parser.add_argument("--output", "-o", choices=("text", "json"), default="text")
    parser.add_argument("ticket_id", nargs="?")
    return parser


def build_quality_check(*, prog: str) -> argparse.ArgumentParser:
    """``rebar quality-check <id> [--output json]``."""
    parser = _base(prog)
    parser.add_argument("--output", "-o", choices=("text", "json"), default="text")
    parser.add_argument("ticket_id", nargs="?")
    return parser


def build_summary(*, prog: str) -> argparse.ArgumentParser:
    """``rebar summary <id> [<id> ...] [--output json]``."""
    parser = _base(prog)
    parser.add_argument("--output", "-o", choices=("text", "json"), default="text")
    parser.add_argument("ticket_id", nargs="*")
    return parser


def build_clarity_check(*, prog: str) -> argparse.ArgumentParser:
    """``rebar clarity-check <id> | --stdin [--config <path>]``."""
    parser = _base(prog)
    parser.add_argument("--stdin", action="store_true")
    parser.add_argument("--config")
    parser.add_argument("ticket_id", nargs="?")
    return parser
