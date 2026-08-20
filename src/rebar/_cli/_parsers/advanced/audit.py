"""``rebar audit`` parser factory (RP-05 S2c, census).

A prog-bound argparse rendering of the ``rebar audit`` grammar owned today by the
hand-rolled parser in :func:`rebar._cli._audit_commands.audit_cli`
(``show <ticket> [--output json|text]`` / ``serve [--host ...] [--port ...]``).
Registered for registry census + the AC3 import-isolation probe; the runtime
handler keeps its bespoke argv walk and error text. Only the stdlib and
:mod:`rebar._cli._parser` are imported at module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def build(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar audit`` nested parser bound to ``prog``."""
    parser = build_argument_parser(
        prog=prog, formatter_class=argparse.HelpFormatter, allow_abbrev=False
    )
    subs = parser.add_subparsers(dest="subcommand", metavar="{show,serve}")

    show = subs.add_parser("show", help="print a ticket's audit trail", allow_abbrev=False)
    show.add_argument("ticket", help="the ticket to audit")
    show.add_argument(
        "--output", choices=("json", "text"), default="json", help="output format (default: json)"
    )

    serve = subs.add_parser(
        "serve", help="start the optional read-only audit web UI", allow_abbrev=False
    )
    serve.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8765, help="bind port (default: 8765)")
    return parser
