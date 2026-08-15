"""Tier B leaf-write command implementations + CLI dispatcher.

The library/MCP call the command functions (``leaf.comment`` etc.) in-process; the
CLI reaches the same functions via :func:`main`. One implementation, two callers —
the Tier A read-path shape applied to writes.

Each entry pins the command's argv arity and usage string, so a too-few-args
invocation prints the canonical ``Usage:`` line and exits 1. These commands take
positionals ONLY: an option-looking token or a surplus positional is a loud usage
error (never stored as data, never dropped), and ``--`` ends option parsing so a
value that legitimately begins with ``-`` can still be written.
"""

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


# Registry of leaf-write commands, keyed by subcommand name.
# min_args / usage pin each command's arity guard.
_REGISTRY: dict[str, _Cmd] = {
    "comment": _Cmd(leaf.comment, 2, "Usage: rebar comment <ticket_id> <body>"),
    "set-verify-commands": _Cmd(
        leaf.set_verify_commands,
        2,
        "Usage: rebar set-verify-commands <ticket_id> <json_array>",
    ),
    "tag": _Cmd(leaf.tag, 2, "Usage: rebar tag <ticket_id> <tag>"),
    "untag": _Cmd(leaf.untag, 2, "Usage: rebar untag <ticket_id> <tag>"),
    "archive": _Cmd(leaf.archive, 1, "Usage: rebar archive <ticket_id>", max_args=1),
}

# These commands take positionals ONLY. An option-looking token is therefore a
# mistake — historically it was silently stored AS DATA (and any surplus token
# holding the intended value was dropped by an `args[:min_args]` slice), so
# `rebar comment <id> --body-file notes.md` stored the literal "--body-file" and
# exited 0. Both shapes are now loud usage errors; `--` is the escape hatch for a
# value that legitimately begins with "-".
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


# The audited force override for the write-time secret screen (bug e7a9). Only the
# `--flag=<reason>` form is recognised — the same shape as `transition --force=` — so every
# other option-looking token stays the loud usage error bug 00da made it, and a reason is
# structurally impossible to omit. Handled here, above the dispatch, so EVERY write verb
# honours it: a false positive on a `create` description or a `session-log` entry must not
# be a permanent hard block just because that verb parses its own argv.
_ALLOW_SECRET_FLAG = "--allow-secret-pattern"


def _extract_allow_secret(args: list[str]) -> tuple[list[str], str]:
    """Pull ``--allow-secret-pattern=<reason>`` out of *args*, returning the rest + reason.

    Runs BEFORE both dispatch routes: ahead of ``_split_positionals`` so the flag is not
    mistaken for the option-looking positional that guard exists to reject, and ahead of
    the self-parsing composers so they never see an option they do not know.

    Only the pre-``--`` segment is inspected: everything after the end-of-options marker
    is literal data (bug 00da), so a body that legitimately BEGINS with this flag — the
    documentation of this very feature, for instance — survives verbatim.
    """
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
    from rebar._commands import _confirm

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


def main(argv: list[str]) -> int:
    """CLI entry for the leaf-write route.

    ``argv`` is ``[<command>, <args>...]``. Returns the process exit code; a
    :class:`CommandError` prints its message to stderr and yields its return code.
    """
    if not argv:
        print("Usage: ticket-commands.py <command> [args...]", file=sys.stderr)
        return 1
    command, args = argv[0], argv[1:]
    # One extraction covering BOTH dispatch routes, so the override the refusal advertises
    # actually exists for every write verb (bug e7a9).
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
    # Every command on this path consumed only its first min_args positionals, so a
    # surplus token was silently DROPPED — including the value a mistyped option was
    # meant to carry. Treat max_args=None as "exactly min_args" and fail loudly.
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
            out = entry.func(*positionals)
    except CommandError as exc:
        print(exc.message, file=sys.stderr)
        return exc.returncode
    # Mutation confirmation (ticket 6bda-9d58-8546-4638): each leaf function
    # returns its small outcome; the formatter prints the kubectl-style line (or
    # the --output json envelope) on the CLI's behalf, so library/MCP callers of
    # the same functions stay print-free.
    from rebar._commands import _confirm

    _confirm.leaf_confirm(command, out)
    return 0
