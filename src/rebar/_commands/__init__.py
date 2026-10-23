"""Leaf write commands and their CLI dispatcher.
Library and MCP callers use the same functions in-process. CLI registry
entries enforce positional arity. Option-looking or surplus arguments fail
with usage text. -- permits literal values beginning with a dash."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import NamedTuple

from rebar._commands import attach_commits as _attach_commits
from rebar._commands import composer, leaf
from rebar._commands import doctor as _doctor
from rebar._commands import idea as _idea
from rebar._commands import session_log as _session_log
from rebar._commands import unlink as _unlink
from rebar._commands._seam import CommandError, forced_secret_write


class _Cmd(NamedTuple):
    func: Callable[..., object]
    min_args: int
    usage: str
    # Max positional args; None = unbounded. Most leaf commands ignore extra args
    # (a too-few-args guard only); archive is exactly-one, so a surplus positional
    # is a usage error. max_args pins that difference.
    max_args: int | None = None
    # RP-05 S2b: the accepted-grammar factory for this leaf (an attribute of
    # ``rebar._cli._parsers.core.writes``) plus its ordered positional dests. The
    # bespoke reject/arity guards below stay for byte-exact diagnostics; the factory
    # is the single source of the ACCEPTED positional grammar the command executes on.
    factory: str = ""
    dests: tuple[str, ...] = ()


# Registry of leaf-write commands, keyed by subcommand name.
# min_args / usage pin each command's arity guard.
_REGISTRY: dict[str, _Cmd] = {
    "comment": _Cmd(
        leaf.comment,
        2,
        "Usage: rebar comment <ticket_id> <body>",
        factory="build_comment",
        dests=("ticket_id", "body"),
    ),
    "set-verify-commands": _Cmd(
        leaf.set_verify_commands,
        2,
        "Usage: rebar set-verify-commands <ticket_id> <json_array>",
        factory="build_set_verify_commands",
        dests=("ticket_id", "json_array"),
    ),
    "archive": _Cmd(
        leaf.archive,
        1,
        "Usage: rebar archive <ticket_id>",
        max_args=1,
        factory="build_archive",
        dests=("ticket_id",),
    ),
}

# Reject option-looking and surplus positionals instead of storing or
# dropping them. Use -- before a literal value beginning with a dash.
_ESCAPE_HINT = (
    'If the value itself begins with "-", end option parsing with "--" first:\n'
    '  rebar {command} {leading}-- "<value>"'
)

# Per-command "how to pass a long/awkward value" hint. `comment` gets the
# cat-substitution form because a lost comment body is the failure that motivated
# this guard; other commands fall back to the generic line below.
_VALUE_HINT: dict[str, str] = {
    "comment": (
        "This command takes the body as a single positional argument. For a long\n"
        "body, substitute a file's contents in the shell:\n"
        '  rebar comment <ticket_id> "$(cat /path/to/body.md)"'
    ),
}
_GENERIC_VALUE_HINT = "This command takes its values as positional arguments only."


def _usage_error(command: str, entry: _Cmd, problem: str) -> str:
    """Compose a loud usage error: what went wrong, the canonical usage line, how
    to pass the value correctly, and the ``--`` escape hatch."""
    # Every command here leads with <ticket_id>; when that IS the only positional
    # (archive) the separator comes first instead.
    leading = "<ticket_id> " if entry.min_args > 1 else ""
    return "\n".join(
        [
            problem,
            entry.usage,
            _VALUE_HINT.get(command, _GENERIC_VALUE_HINT),
            _ESCAPE_HINT.format(command=command, leading=leading),
        ]
    )


def _split_positionals(args: list[str]) -> tuple[list[str], str | None]:
    """Split ``args`` into positionals, honouring ``--`` as end-of-options.

    Returns ``(positionals, offending_token)``. The FIRST bare ``--`` is consumed
    and every token after it is a positional verbatim. Before that separator, any
    token beginning with ``-`` is rejected (returned as ``offending_token``) rather
    than being accepted as data.
    """
    positionals: list[str] = []
    for index, token in enumerate(args):
        if token == "--":
            positionals.extend(args[index + 1 :])
            return positionals, None
        if token.startswith("-"):
            return positionals, token
        positionals.append(token)
    return positionals, None


# Extract only the reason-bearing secret-screen override above both write
# dispatch routes. Other option-looking tokens remain usage errors.
_ALLOW_SECRET_FLAG = "--allow-secret-pattern"


def _extract_allow_secret(args: list[str]) -> tuple[list[str], str]:
    """Extract a reason-bearing secret-screen override before dispatch.
    Inspect only arguments preceding bare --. Later text stays literal data."""
    head = args[: args.index("--")] if "--" in args else args
    tail = args[len(head) :]
    reason, rest = "", []
    for token in head:
        if token.startswith(f"{_ALLOW_SECRET_FLAG}="):
            reason = token.split("=", 1)[1]
            continue
        rest.append(token)
    return rest + tail, reason


_SET_FILE_IMPACT_USAGE = (
    "Usage: rebar set-file-impact <ticket_id> <json_array>\n"
    '   or: rebar set-file-impact <ticket_id> --none "<reason>"'
)


def _set_file_impact_cli(args: list[str]) -> int:
    """Parse the two mutually-exclusive file-impact write forms."""
    from rebar._cli._parsers.core.writes import build_set_file_impact
    from rebar._commands import _confirm

    # Parser of record for the accepted grammar; the explicit arity/``--none`` shape
    # below is retained because the two mutually-exclusive forms and the exact usage
    # exit code are not expressible as a single argparse spec.
    build_set_file_impact(prog="rebar set-file-impact").parse_known_args(args)

    if len(args) == 2 and args[1] != "--none":
        out = leaf.set_file_impact(args[0], args[1])
        _confirm.emit(
            "file-impact-set",
            out["id"],
            f"{out['count']} paths",
            f"impact set on {out['id']}: {out['count']} paths",
        )
        return 0
    if len(args) == 3 and args[1] == "--none":
        out = leaf.declare_no_file_impact(args[0], args[2])
        _confirm.emit(
            "file-impact-set",
            out["id"],
            "none declared",
            f"impact set on {out['id']}: none declared",
        )
        return 0
    raise CommandError(_SET_FILE_IMPACT_USAGE, returncode=2)


# Variadic commands that parse their own full argv (flags, --output) and return an
# exit code directly — the heavier event-composers (docs/bash-migration.md §4).
_ARGV_REGISTRY: dict[str, Callable[[list[str]], int]] = {
    "set-file-impact": _set_file_impact_cli,
    "attach-commits": _attach_commits.attach_commits_cli,
    "create": composer.create_cli,
    "idea": _idea.idea_cli,
    "edit": composer.edit_cli,
    "link": composer.link_cli,
    "unlink": _unlink.unlink_cli,
    "doctor": _doctor.doctor_cli,
    "revert": composer.revert_cli,
    "session-log": _session_log.session_log_cli,
}


def _parse_leaf_positionals(command: str, entry: _Cmd, positionals: list[str]) -> list[str]:
    """Route the (already reject-guarded, arity-checked) positionals through the leaf's
    accepted-grammar factory (RP-05 S2b) and return the ordered positional values that
    drive ``entry.func``.

    The bespoke ``_split_positionals`` reject guard and the ``min_args``/``max_args``
    arity checks upstream own the byte-exact diagnostics; this only makes the factory the
    parser of record for the ACCEPTED grammar. The import is function-local so the factory
    can be spied at its defining module.
    """
    from rebar._cli._parsers.core import writes as _writes_parsers

    factory = getattr(_writes_parsers, entry.factory)
    # ``positionals`` are already end-of-options resolved by ``_split_positionals``
    # (tokens after ``--`` are literal data, even when dash-leading). Prepend ``--``
    # so the factory's argparse treats every one as a positional value rather than
    # re-interpreting a dash-leading datum as an option.
    ns, _extra = factory(prog=f"rebar {command}").parse_known_args(["--", *positionals])
    return [getattr(ns, dest) for dest in entry.dests]


def main(argv: list[str]) -> int:
    """CLI entry for the leaf-write route.

    ``argv`` is ``[<command>, <args>...]``. Returns the process exit code; a
    :class:`CommandError` prints its message to stderr and yields its return code.
    """
    if not argv:
        print("Usage: ticket-commands.py <command> [args...]", file=sys.stderr)
        return 1
    command, args = argv[0], argv[1:]
    # Extract the override once for both write dispatch routes.
    args, allow_secret_pattern = _extract_allow_secret(args)
    argv_handler = _ARGV_REGISTRY.get(command)
    if argv_handler is not None:
        try:
            with forced_secret_write(allow_secret_pattern):
                return argv_handler(args)
        except CommandError as exc:
            print(exc.message, file=sys.stderr)
            return exc.returncode
    entry = _REGISTRY.get(command)
    if entry is None:
        print(f"Error: unknown leaf-write command '{command}'", file=sys.stderr)
        return 1
    positionals, offending = _split_positionals(args)
    if offending is not None:
        print(
            _usage_error(
                command,
                entry,
                f"Error: unrecognised option '{offending}' — 'rebar {command}' accepts no options.",
            ),
            file=sys.stderr,
        )
        return 2
    if len(positionals) < entry.min_args:
        print(entry.usage, file=sys.stderr)
        return 1
    # Reject surplus positionals instead of silently dropping them.
    limit = entry.max_args if entry.max_args is not None else entry.min_args
    if len(positionals) > limit:
        print(
            _usage_error(
                command,
                entry,
                f"Error: 'rebar {command}' takes exactly {limit} argument(s), "
                f"got {len(positionals)}; refusing to drop "
                f"{', '.join(repr(a) for a in positionals[limit:])}.",
            ),
            file=sys.stderr,
        )
        return 2
    try:
        with forced_secret_write(allow_secret_pattern):
            out = entry.func(*_parse_leaf_positionals(command, entry, positionals))
    except CommandError as exc:
        print(exc.message, file=sys.stderr)
        return exc.returncode
    # CLI formats leaf outcomes. Library and MCP calls remain print-free.
    from rebar._commands import _confirm

    _confirm.leaf_confirm(command, out)
    return 0
