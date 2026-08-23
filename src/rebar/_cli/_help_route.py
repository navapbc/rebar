"""Top-level help / overview / unknown pre-scan for the ``rebar`` CLI (RP-05 S2d).

The canonical package help lives under ``rebar/_cli/help/*.txt`` (generated from the parser
factories by ``scripts/gen_cli_help.py`` and served verbatim by :mod:`rebar._cli._help`).
This module owns the LEXICAL pre-scan that lets ``main()`` answer a help/overview/unknown
request from those committed bytes **before** any RP-04 operation snapshot, config
parsing/materialization, store mount, handler/factory resolution, or optional import — a help
request must never compose an operation snapshot or leak a ``.tickets-tracker`` into the repo
(bug dd62).

The pre-scan is deliberately narrow. It recognizes:

* It skips the leading global config prefix (``-c SECTION.KEY=VALUE`` or ``--config=…``)
  without materializing configuration.
* It recognizes the top-level help forms ``help``, ``--help``, ``-h``, and
  ``help <command>``.
* It recognizes a help request (``--help`` or ``-h``) for a visible subcommand that is not
  retired while preserving child help for nested families.
* It recognizes an unknown subcommand.

Everything else is a command invocation or a nested child help request. Those forms return
``None`` and continue to dispatch.
"""

from __future__ import annotations

import sys

from rebar._cli import _help
from rebar._cli._registry import ROUTES, Route, route_for

# Nested dispatch families keep nonleading child help in their parser or handler. The bridge
# family is derived from its shared parser factory. Pure intercept families are named because
# the pre-scan cannot resolve parser factories. Config is explicit because ``validate`` is a
# handler-dispatched pseudo-subcommand rather than an argparse subparser.
_BRIDGE_FACTORY: str | None = next((r.parser_factory for r in ROUTES if r.name == "bridge"), None)
_NESTED_INTERCEPTS: frozenset[str] = frozenset(
    {"audit", "config", "criteria", "identity", "llm", "prompt", "workflow"}
)
_NESTED_FAMILY: frozenset[str] = frozenset(
    r.name
    for r in ROUTES
    if (_BRIDGE_FACTORY is not None and r.parser_factory == _BRIDGE_FACTORY)
    or r.name in _NESTED_INTERCEPTS
)
# Hidden alias spellings (e.g. ``bridge-status``) are neither advertised nor help-served here.
_HIDDEN_ALIASES: frozenset[str] = frozenset(r.name for r in ROUTES if r.hidden)


def _help_backed(route: Route) -> bool:
    """Return whether ``route`` is visible, not retired, and carries committed help."""
    return not route.hidden and not route.retired


def wants_help(rest: list[str]) -> bool:
    """True if a bare ``--help``/``-h`` appears before any ``--`` terminator.

    The dispatcher must honour a help flag in ANY position, not only ``rest[0]``:
    ``rebar create task --help`` used to fall through to the create handler, which consumed
    ``--help`` as the positional title and created a placeholder ticket (bug b8de). Scanning
    stops at the first ``--`` so a caller can suppress the help intercept at the dispatcher.
    """
    for tok in rest:
        if tok == "--":
            return False
        if tok in ("--help", "-h"):
            return True
    return False


def help_requested(sub: str, rest: list[str]) -> bool:
    """Whether ``rebar <sub> …`` is a help request the pre-scan should serve itself.

    Nested dispatch families render child help through their parser or handler. Only a
    leading ``--help`` or ``-h`` asks for the family's usage. Every other command treats a
    help flag before ``--`` as a usage request.
    """
    if sub in _NESTED_FAMILY:
        return bool(rest) and rest[0] in ("--help", "-h")
    return wants_help(rest)


def emit_subcommand_help(sub: str) -> int:
    """Print ``sub``'s pinned usage.

    A known visible subcommand writes help to stdout and returns zero. An unknown subcommand
    writes an error and the overview to stderr and returns one.
    """
    text = _help.subcommand_help(sub)
    if text is not None:
        sys.stdout.write(text)
        return 0
    sys.stderr.write(f"Error: unknown subcommand '{sub}'\n\n")
    sys.stderr.write(_help.overview())
    return 1


def _valid_override(value: str) -> bool:
    """Whether a config-prefix value has the ``SECTION.KEY=VALUE`` shape (lexically)."""
    return "=" in value and not value.startswith("-")


def _strip_config_prefix(argv: list[str]) -> list[str] | None:
    """Skip WELL-FORMED leading ``-c``/``--config`` tokens, returning the residual argv.

    Returns ``None`` when the prefix is malformed (a missing or non ``SECTION.KEY=VALUE``
    value) so the real config parser in ``_main_dispatch`` produces its exact error rather
    than the pre-scan guessing at a help form.
    """
    out = list(argv)
    while out and (out[0] in ("-c", "--config") or out[0].startswith("--config=")):
        tok = out.pop(0)
        if tok.startswith("--config="):
            value = tok[len("--config=") :]
        elif out:
            value = out.pop(0)
        else:
            return None
        if not _valid_override(value):
            return None
    return out


def _emit_unknown(sub: str) -> int:
    """The bare unknown-subcommand contract: error to stderr + overview to stdout, exit 1."""
    sys.stderr.write(f"Error: unknown subcommand '{sub}'\n")
    sys.stdout.write(_help.overview())
    return 1


def pre_scan(argv: list[str]) -> int | None:
    """Serve a help/overview/unknown request from committed bytes, or ``None`` to fall through.

    Runs BEFORE any operation snapshot, config materialization, store mount, handler/factory
    resolution, or optional import. It returns the process exit code when it handles the
    request. It returns ``None`` for a command invocation or nested child help request.
    """
    residual = _strip_config_prefix(argv)
    if residual is None:
        return None
    if not residual:
        sys.stdout.write(_help.overview())
        return 1

    first = residual[0]
    if first in ("help", "--help", "-h"):
        if len(residual) >= 2:
            return emit_subcommand_help(residual[1])
        sys.stdout.write(_help.overview())
        return 0

    sub, rest = first, residual[1:]
    route = route_for(sub)
    if route is not None and _help_backed(route) and sub not in _HIDDEN_ALIASES:
        if help_requested(sub, rest):
            return emit_subcommand_help(sub)
        return None
    if route is None and sub not in _HIDDEN_ALIASES:
        return _emit_unknown(sub)
    return None
