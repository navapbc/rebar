"""``rebar store`` parser factory: the storage-maintenance family.

A prog-bound argparse rendering of ``rebar store <sub>``. Each member's grammar stays with
the parser factory its route in :data:`rebar._cli._registry.STORE_ROUTES` names; this parser
only names the members, for the family help and the invalid-choice error, and each member
line repeats the first line of that member factory's description. Only the stdlib and
:mod:`rebar._cli._parser` are imported at module top level; the registry and the member
factories are imported when the parser is built.
"""

from __future__ import annotations

import argparse
import importlib

from rebar._cli._parser import RebarHelpFormatter, build_argument_parser


def _summary(ref: str, prog: str) -> str:
    """The first line of the description of the parser that ``ref`` builds."""
    module, _, attr = ref.partition(":")
    member = getattr(importlib.import_module(module), attr)(prog=prog)
    description = (member.description or "").strip()
    return " ".join(description.splitlines()[0].split()) if description else ""


def build(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar store`` family parser bound to ``prog``."""
    from rebar._cli._registry import STORE_ROUTES

    parser = build_argument_parser(
        prog=prog,
        description="Check, repair, measure, migrate, and compact the ticket store.",
        epilog="Run 'rebar store <subcommand> --help' for a subcommand's options.",
        formatter_class=RebarHelpFormatter,
        allow_abbrev=False,
    )
    names = ",".join(route.name for route in STORE_ROUTES)
    subs = parser.add_subparsers(
        dest="subcommand", required=True, title="commands", metavar=f"{{{names}}}"
    )
    for route in STORE_ROUTES:
        if route.parser_factory is None:
            continue
        subs.add_parser(
            route.name,
            help=_summary(route.parser_factory, f"{prog} {route.name}"),
            add_help=False,
            allow_abbrev=False,
        )
    return parser
