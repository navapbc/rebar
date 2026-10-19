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
# factory. Intercepts, the ``store`` family (whose members render their own help), and
# config's handler-only ``validate`` are named because pre-scan cannot derive them.
_BRIDGE_FACTORY: str | None = next((r.parser_factory for r in ROUTES if r.name == "bridge"), None)
_NESTED_INTERCEPTS: frozenset[str] = frozenset(
    {"audit", "config", "criteria", "identity", "llm", "prompt", "store", "workflow"}
)
_NESTED_FAMILY: frozenset[str] = frozenset(
    r.name
    for r in ROUTES
    if (_BRIDGE_FACTORY is not None and r.parser_factory == _BRIDGE_FACTORY)
    or r.name in _NESTED_INTERCEPTS
)
# Hidden alias spellings (e.g. ``bridge-status``) are neither advertised nor help-served here.
_HIDDEN_ALIASES: frozenset[str] = frozenset(r.name for r in ROUTES if r.hidden)
# Removed spellings -> their replacement (hard cutover, no working aliases). Each fails
# loudly through argparse's invalid-choice error plus a hint naming the replacement.
_REMOVED_SPELLINGS: dict[str, str] = {
    # Pre-1.0 simple compatibility aliases (docs/exit-codes.md).
    "bridge-status": "rebar bridge status",
    "bridge-fsck": "rebar bridge fsck",
    "bridge-probe": "rebar bridge check-access",
    "jira-onboard": "rebar bridge setup",
    "verify-authorship": "rebar verify-identity",
    # Ticket verbs merged into the golden-path verbs (epic toadyish-sulfurous-ovenbird).
    "exists": "rebar resolve <id>",
    "format": "rebar resolve <id> --display[=MODE]",
    "check-ac": "rebar check <id> --ac",
    "clarity-check": "rebar check <id> --clarity",
    "quality-check": "rebar check <id>",
    "tag": "rebar edit <id> --add-tag <tag>",
    "untag": "rebar edit <id> --remove-tag <tag>",
    "get-file-impact": "rebar show <id> --field=file_impact",
    "get-verify-commands": "rebar show <id> --field=verify_commands",
    "list-descendants": "rebar deps <id> --descendants",
    "session-logs": "rebar list --type=session_log --sort=-created --limit=5",
    # Storage-maintenance verbs moved under the ``store`` family.
    "fsck": "rebar store fsck",
    "fsck-recover": "rebar store fsck-recover",
    "doctor": "rebar store doctor",
    "tracker-maintenance": "rebar store maintenance",
    "tracker-footprint": "rebar store footprint",
    "export": "rebar store export",
    "import": "rebar store import",
    "compact": "rebar store compact <id>",
    "compact-all": "rebar store compact --all",
}
_REMOVED_SIMPLE_CLI_ALIASES: frozenset[str] = frozenset(_REMOVED_SPELLINGS)


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
    """Render a removed spelling through argparse's standard invalid-choice error, then a
    hint naming its replacement. Returns argparse's usage-error exit (2)."""
    parser = argparse.ArgumentParser(prog="rebar")
    parser.add_argument(
        "subcommand",
        choices=tuple(r.name for r in ROUTES if not r.hidden and not r.retired),
    )
    code = 1
    try:
        parser.parse_args([sub])
    except SystemExit as exc:
        code = int(exc.code or 0)
    replacement = _REMOVED_SPELLINGS.get(sub)
    if replacement is not None:
        sys.stderr.write(f"hint: 'rebar {sub}' was removed; use `{replacement}` instead\n")
    return code


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
