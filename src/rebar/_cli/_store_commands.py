"""Implement ``rebar store <sub>``, the storage-maintenance command family.

The top-level ``store`` route initializes nothing. Each member in
:data:`rebar._cli._registry.STORE_ROUTES` keeps the handler, adapter, and init policy it had
as a top-level command, so routing a member through :func:`rebar._cli._execute.execute`
applies exactly that member's policy: ``doctor`` initializes read-only unless ``--repair``,
``fsck-recover`` skips initialization under an injected tracker, ``export`` initializes
without reconverging, and ``footprint`` never mounts or initializes.

The family help (``rebar store --help``) is the committed ``help/store.txt`` that the help
pre-scan serves. A member's help is rendered from its parser factory with
``prog="rebar store <sub>"``. A missing or unknown member is a usage error (exit 2).
"""

from __future__ import annotations

import importlib
import sys

_USAGE = (
    "Usage: rebar store <subcommand> [args...]\nRun 'rebar store --help' for the subcommands.\n"
)


def store_cli(rest: list[str]) -> int:
    """Entry point for ``rebar store …``. Returns the process exit code."""
    if not rest:
        sys.stderr.write(_USAGE)
        return 2
    sub, sub_rest = rest[0], rest[1:]
    if sub in ("--help", "-h"):
        from rebar._cli import _help

        sys.stdout.write(_help.subcommand_help("store") or _USAGE)
        return 0

    from rebar._cli._registry import STORE_ROUTES, route_for

    route = route_for(sub, STORE_ROUTES)
    if route is None or route.parser_factory is None:
        return _invalid_choice(sub)

    from rebar._cli._help_route import wants_help

    if wants_help(sub_rest):
        return _member_help(route.name, route.parser_factory)

    from rebar._cli import _execute

    return _execute.execute(sub, sub_rest, routes=STORE_ROUTES)


def _member_help(name: str, factory_ref: str) -> int:
    """Print the member's parser help under ``rebar store <name>``; exit 0.

    The ``usage:`` prefix is capitalized and the text ends in exactly one newline, the
    same byte policy ``scripts/gen_cli_help.py`` applies to the committed artifacts.
    """
    module, _, attr = factory_ref.partition(":")
    factory = getattr(importlib.import_module(module), attr)
    text = factory(prog=f"rebar store {name}").format_help()
    if text.startswith("usage: "):
        text = "Usage: " + text[len("usage: ") :]
    sys.stdout.write(text.rstrip("\n") + "\n")
    return 0


def _invalid_choice(sub: str) -> int:
    """Render argparse's invalid-choice error for an unknown member; exit 2."""
    from rebar._cli._parser import ParseError, render_parse_error
    from rebar._cli._parsers.advanced.store import build

    try:
        build(prog="rebar store").parse_args([sub])
    except ParseError as exc:
        return render_parse_error(exc)
    sys.stderr.write(_USAGE)
    return 2
