"""Serve top-level help, overview, and unknown commands before stateful CLI work.

The lexical pre-scan reads committed help bytes without materializing configuration,
binding a snapshot, mounting a store, or resolving lazy imports. It skips well-formed
leading configuration overrides. It recognizes top-level, visible-command, and unknown
forms. It leaves nested child help and executable commands for dispatch.
"""

from __future__ import annotations

import argparse
import sys

from rebar._cli import _help
from rebar._cli._registry import ROUTES, Route, route_for

# Nested families retain nonleading child help. Bridge membership comes from its parser
# factory. Intercepts and config's handler-only ``validate`` are named because pre-scan
# cannot derive them.
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
_HISTORICAL_HIDDEN_ALIASES: frozenset[str] = frozenset({"bridge-status"})
_HISTORICAL_VISIBLE_ALIASES: frozenset[str] = frozenset(
    {"bridge-fsck", "bridge-probe", "jira-onboard", "verify-authorship"}
)
_REMOVED_SIMPLE_CLI_ALIASES: frozenset[str] = (
    _HISTORICAL_HIDDEN_ALIASES | _HISTORICAL_VISIBLE_ALIASES
)


def _help_backed(route: Route) -> bool:
    """Return whether ``route`` is visible, not retired, and carries committed help."""
    return not route.hidden and not route.retired


def wants_help(rest: list[str]) -> bool:
    """Return whether ``--help`` or ``-h`` occurs before ``--``.

    Any-position scanning prevents positional parsers from consuming help. ``--``
    explicitly suppresses the intercept.
    """
    for tok in rest:
        if tok == "--":
            return False
        if tok in ("--help", "-h"):
            return True
    return False


def help_requested(sub: str, rest: list[str]) -> bool:
    """Return whether pre-scan should serve help for this route.

    Nested families expose only leading help here. Other commands accept a help flag
    anywhere before ``--``.
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
    """Return argv after well-formed leading config overrides.

    Return ``None`` for a malformed prefix so the command parser emits its exact error.
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


def _emit_removed_alias_invalid_choice(sub: str) -> int:
    """Render retired simple aliases through argparse's standard invalid-choice error."""
    parser = argparse.ArgumentParser(prog="rebar")
    parser.add_argument(
        "subcommand",
        choices=tuple(r.name for r in ROUTES if not r.hidden and not r.retired),
    )
    try:
        parser.parse_args([sub])
    except SystemExit as exc:
        return int(exc.code or 0)
    return 1


def pre_scan(argv: list[str]) -> int | None:
    """Serve help, overview, or unknown forms before stateful work.

    Return their exit code, or ``None`` for commands and nested child help that must
    dispatch.
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
    if sub in _REMOVED_SIMPLE_CLI_ALIASES:
        return _emit_removed_alias_invalid_choice(sub)
    route = route_for(sub)
    if route is not None and _help_backed(route) and sub not in _HIDDEN_ALIASES:
        if help_requested(sub, rest):
            return emit_subcommand_help(sub)
        return None
    if route is None and sub not in _HIDDEN_ALIASES:
        return _emit_unknown(sub)
    return None
