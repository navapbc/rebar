"""``rebar transition`` / ``reopen`` / ``claim`` parser factories (RP-05 S2b).

Prog-bound argparse renderings of the three lifecycle arms in
:mod:`rebar._commands.transition` and :mod:`rebar._commands.claim`. All honour the
``report`` ``--output`` profile. ``transition`` models the ``<id> [<current>]
<target>`` positionals plus its close/force flag surface; ``reopen`` takes just an
id; ``claim`` takes an id plus ``--assignee`` / ``--force`` / ``--review``.

The handlers keep their bespoke parsing intact — in particular ``transition`` and
``claim`` retain their hand-rolled inline-only ``--force[=<reason>]`` extraction
(argparse cannot express "optional value that never swallows the next token") and
their exact ``Usage:`` diagnostics and exit codes. Only the stdlib and
:mod:`rebar._cli._parser` are imported at module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def _base(prog: str) -> argparse.ArgumentParser:
    return build_argument_parser(prog=prog, add_help=False, allow_abbrev=False)


def build_transition(*, prog: str) -> argparse.ArgumentParser:
    """``rebar transition <id> [<current>] <target> [flags]``."""
    parser = _base(prog)
    parser.add_argument("--output", "-o", choices=("text", "json"), default="text")
    parser.add_argument("--reason", default="")
    parser.add_argument("--class", dest="close_class", default="")
    parser.add_argument("--caused-by", dest="caused_by", default="")
    parser.add_argument("--ref", default="")
    # Inline-only escape hatch; runtime keeps its own hand-rolled extraction.
    parser.add_argument("--force", nargs="?", const="")
    parser.add_argument("ticket_id", nargs="?")
    parser.add_argument("statuses", nargs="*")
    return parser


def build_reopen(*, prog: str) -> argparse.ArgumentParser:
    """``rebar reopen <ticket_id>``."""
    parser = _base(prog)
    parser.add_argument("--output", "-o", choices=("text", "json"), default="text")
    parser.add_argument("ticket_id", nargs="?")
    return parser


def build_claim(*, prog: str) -> argparse.ArgumentParser:
    """``rebar claim <ticket_id> [--assignee=<name>] [--force[=<reason>]] [--review]``."""
    parser = _base(prog)
    parser.add_argument("--output", "-o", choices=("text", "json"), default="text")
    parser.add_argument("--assignee", default=None)
    parser.add_argument("--force", nargs="?", const="")
    parser.add_argument("--review", action="store_true")
    parser.add_argument("ticket_id", nargs="?")
    return parser
