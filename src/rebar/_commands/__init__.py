"""Tier B leaf-write command implementations + CLI dispatcher.

The library/MCP call the command functions (``leaf.comment`` etc.) in-process; the
bash dispatcher reaches the same functions via :func:`main` (run by the
``ticket-commands.py`` engine entrypoint) when ``REBAR_LEAF_WRITES=python``. One
implementation, two callers — the Tier A read-path shape applied to writes.

Each entry pins the command's argv arity and usage string to the bash function it
replaces, so a too-few-args invocation prints the identical ``Usage:`` line and
exits 1 under either implementation.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import NamedTuple

from rebar._commands import composer, leaf
from rebar._commands import unlink as _unlink
from rebar._commands._seam import CommandError


class _Cmd(NamedTuple):
    func: Callable[..., None]
    min_args: int
    usage: str
    # Max positional args; None = unbounded. The bash leaf functions guard arity
    # with `[ $# -lt N ]` (extra args ignored) except archive's `[ $# -ne 1 ]`
    # (extra args are a usage error). max_args pins that difference.
    max_args: int | None = None


# Registry of ported Tier B commands, keyed by the dispatcher subcommand name.
# min_args / usage mirror the bash `[ $# -lt N ]` guards in ticket-lib-api.sh.
_REGISTRY: dict[str, _Cmd] = {
    "comment": _Cmd(leaf.comment, 2, "Usage: ticket comment <ticket_id> <body>"),
    "set-file-impact": _Cmd(
        leaf.set_file_impact, 2, "Usage: ticket set-file-impact <ticket_id> <json_array>"
    ),
    "set-verify-commands": _Cmd(
        leaf.set_verify_commands,
        2,
        "Usage: ticket set-verify-commands <ticket_id> <json_array>",
    ),
    "tag": _Cmd(leaf.tag, 2, "Usage: ticket tag <ticket_id> <tag>"),
    "untag": _Cmd(leaf.untag, 2, "Usage: ticket untag <ticket_id> <tag>"),
    "archive": _Cmd(leaf.archive, 1, "Usage: ticket archive <ticket_id>", max_args=1),
}

# Variadic commands that parse their own full argv (flags, --output) and return an
# exit code directly — the heavier event-composers (docs/bash-migration.md §4).
_ARGV_REGISTRY: dict[str, Callable[[list[str]], int]] = {
    "create": composer.create_cli,
    "edit": composer.edit_cli,
    "link": composer.link_cli,
    "unlink": _unlink.unlink_cli,
    "revert": composer.revert_cli,
}


def is_ported(command: str) -> bool:
    """True when ``command`` has a Python Tier B implementation registered."""
    return command in _REGISTRY or command in _ARGV_REGISTRY


def main(argv: list[str]) -> int:
    """CLI entry for the bash dispatcher's Python leaf-write route.

    ``argv`` is ``[<command>, <args>...]``. Returns the process exit code; a
    :class:`CommandError` prints its message to stderr and yields its return code
    (mirroring the bash functions' stderr + exit contract).
    """
    if not argv:
        print("Usage: ticket-commands.py <command> [args...]", file=sys.stderr)
        return 1
    command, args = argv[0], argv[1:]
    argv_handler = _ARGV_REGISTRY.get(command)
    if argv_handler is not None:
        try:
            return argv_handler(args)
        except CommandError as exc:
            print(exc.message, file=sys.stderr)
            return exc.returncode
    entry = _REGISTRY.get(command)
    if entry is None:
        print(f"Error: unknown leaf-write command '{command}'", file=sys.stderr)
        return 1
    if len(args) < entry.min_args or (entry.max_args is not None and len(args) > entry.max_args):
        print(entry.usage, file=sys.stderr)
        return 1
    try:
        # Pass exactly min_args positionals — the bash leaf functions read only
        # their first N and ignore extras (where extras are even permitted).
        entry.func(*args[: entry.min_args])
    except CommandError as exc:
        print(exc.message, file=sys.stderr)
        return exc.returncode
    return 0
