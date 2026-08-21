"""``rebar`` write-command parser factories (RP-05 S2b).

Prog-bound argparse renderings of the fourteen leaf-write arms dispatched by
:mod:`rebar._commands` (the ``_REGISTRY`` positional-only leaves and the
``_ARGV_REGISTRY`` self-parsing composers), plus ``session-log``. Each models the
ACCEPTED grammar — the positional ids/bodies and, for the composer verbs, their
flag surface (``create``/``idea`` also honour the legacy ``report`` ``--output``
profile). The strict positional-only leaves (``comment``/``tag``/``untag``/
``archive``/``set-verify-commands``) take positionals ONLY.

The handlers keep their bespoke diagnostics — the positional-only leaves' loud
``unrecognised option '<x>' — 'rebar <cmd>' accepts no options`` rejection, the
composers' ``unrecognised option`` / ``unexpected argument`` wording, and the
``--`` / ``--allow-secret-pattern`` pre-dispatch handling — and exit codes. Only
the stdlib and :mod:`rebar._cli._parser` are imported at module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def _base(prog: str, description: str | None = None) -> argparse.ArgumentParser:
    return build_argument_parser(
        prog=prog, description=description, add_help=False, allow_abbrev=False
    )


def build_create(*, prog: str) -> argparse.ArgumentParser:
    """``rebar create <type> <title> [<parent>] [flags] [--output json]``."""
    parser = _base(prog, "Create a new ticket.")
    parser.add_argument(
        "--output", "-o", choices=("text", "json"), default="text", help="output format"
    )
    parser.add_argument("--parent", help="parent ticket id")
    parser.add_argument("--priority", "-p", help="priority 0-4")
    parser.add_argument("--description", "-d", help="ticket description")
    parser.add_argument("--assignee", help="assignee (Jira-resolvable)")
    parser.add_argument("--tags", help="comma-separated tags")
    parser.add_argument("--bridge-project", help="bridge project key")
    parser.add_argument("--repos", help="comma-separated repos for the bridge project")
    parser.add_argument("--detected-by", help="detected_by:<gate> tag (bugs)")
    parser.add_argument("ticket_type", nargs="?", help="bug|epic|story|task")
    parser.add_argument("title", nargs="?", help="ticket title")
    parser.add_argument("parent", nargs="?", help="optional parent id (positional)")
    return parser


def build_idea(*, prog: str) -> argparse.ArgumentParser:
    """``rebar idea <title> [--description=<text>] [--output json]``."""
    parser = _base(
        prog,
        "Capture an undesigned idea (creates an epic in status 'idea'; excluded from "
        "ready/next-batch).",
    )
    parser.add_argument(
        "--output", "-o", choices=("text", "json"), default="text", help="output format"
    )
    parser.add_argument("--description", "-d", help="idea description")
    parser.add_argument("title", nargs="?", help="idea title")
    return parser


def build_comment(*, prog: str) -> argparse.ArgumentParser:
    """``rebar comment <ticket_id> <body>`` (positionals only)."""
    parser = _base(prog, "Add a comment to a ticket.")
    parser.add_argument("ticket_id", nargs="?", help="the ticket to comment on")
    parser.add_argument("body", nargs="?", help="the comment body")
    return parser


def build_link(*, prog: str) -> argparse.ArgumentParser:
    """``rebar link <id1> <id2> <relation> [--dry-run]``."""
    parser = _base(
        prog,
        "Link two tickets (relation REQUIRED: "
        "blocks|depends_on|relates_to|duplicates|supersedes|discovered_from|caused_by).",
    )
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("source", nargs="?", help="source ticket")
    parser.add_argument("target", nargs="?", help="target ticket")
    parser.add_argument("relation", nargs="?", help="the required relation")
    return parser


def build_unlink(*, prog: str) -> argparse.ArgumentParser:
    """``rebar unlink <id1> <id2> [<relation>]``."""
    parser = _base(
        prog,
        "Remove a link: unlink <source> <target> [relation] "
        "(no relation: most-recent for pair; relation: exactly that relation).",
    )
    parser.add_argument("source", nargs="?", help="source ticket")
    parser.add_argument("target", nargs="?", help="target ticket")
    parser.add_argument("relation", nargs="?", help="optional relation selector")
    return parser


def build_revert(*, prog: str) -> argparse.ArgumentParser:
    """``rebar revert <ticket_id> <target_uuid> [--reason=<text>]``."""
    parser = _base(prog, "Revert a ticket to a prior event UUID.")
    parser.add_argument("--reason", default="", help="reason recorded with the revert")
    parser.add_argument("ticket_id", nargs="?", help="the ticket to revert")
    parser.add_argument("target_uuid", nargs="?", help="the event UUID to revert to")
    return parser


def build_edit(*, prog: str) -> argparse.ArgumentParser:
    """``rebar edit <ticket_id> [--<field>=VALUE ...] [tag deltas] [--review]``."""
    parser = _base(
        prog,
        "Edit ticket fields (--title, --priority, --assignee, --ticket_type, --description, "
        "--parent; tags via --add-tag/--remove-tag/--set-tags).",
    )
    parser.add_argument("--title", help="new title")
    parser.add_argument("--priority", help="new priority 0-4")
    parser.add_argument("--assignee", help="new assignee (Jira-resolvable)")
    parser.add_argument("--ticket_type", help="new ticket type")
    parser.add_argument("--description", help="new description")
    parser.add_argument("--parent", help="new parent id")
    parser.add_argument("--bridge-project", help="bridge project key")
    parser.add_argument("--add-tag", help="add a tag")
    parser.add_argument("--remove-tag", help="remove a tag")
    parser.add_argument("--set-tags", help="set the full tag set (add-wins)")
    parser.add_argument("--review", action="store_true", help="emit the review payload")
    parser.add_argument("ticket_id", nargs="?", help="the ticket to edit")
    parser.epilog = "--review: preview payload, not atomic; see review-plan --status."
    return parser


def build_tag(*, prog: str) -> argparse.ArgumentParser:
    """``rebar tag <ticket_id> <tag>`` (positionals only)."""
    parser = _base(prog, "Add a tag to a ticket.")
    parser.add_argument("ticket_id", nargs="?", help="the ticket to tag")
    parser.add_argument("tag", nargs="?", help="the tag to add")
    return parser


def build_untag(*, prog: str) -> argparse.ArgumentParser:
    """``rebar untag <ticket_id> <tag>`` (positionals only)."""
    parser = _base(prog, "Remove a tag from a ticket.")
    parser.add_argument("ticket_id", nargs="?", help="the ticket to untag")
    parser.add_argument("tag", nargs="?", help="the tag to remove")
    return parser


def build_archive(*, prog: str) -> argparse.ArgumentParser:
    """``rebar archive <ticket_id>`` (single positional)."""
    parser = _base(prog, "Archive an open ticket (excludes from default list; idempotent).")
    parser.add_argument("ticket_id", nargs="?", help="the ticket to archive")
    return parser


def build_set_file_impact(*, prog: str) -> argparse.ArgumentParser:
    """``rebar set-file-impact <id> <json_array> | <id> --none <reason>``."""
    parser = _base(
        prog,
        "Record file impact for a ticket (JSON array of {path,reason} objects).",
    )
    parser.epilog = 'No-impact form: rebar set-file-impact <ticket_id> --none "<reason>".'
    parser.add_argument(
        "--none",
        dest="none_reason",
        nargs="?",
        const="",
        help="record 'no file impact' with a reason",
    )
    parser.add_argument("ticket_id", nargs="?", help="the ticket to annotate")
    parser.add_argument(
        "value",
        nargs="?",
        metavar="<json_array>",
        help="JSON array of {path,reason} objects",
    )
    return parser


def build_set_verify_commands(*, prog: str) -> argparse.ArgumentParser:
    """``rebar set-verify-commands <ticket_id> <json_array>`` (positionals only)."""
    parser = _base(prog, "Record DD-level verify commands (JSON array of {dd_id,dd_text,command}).")
    parser.add_argument("ticket_id", nargs="?", help="the ticket to annotate")
    parser.add_argument("json_array", nargs="?", help="JSON array of {dd_id,dd_text,command}")
    return parser


def build_attach_commits(*, prog: str) -> argparse.ArgumentParser:
    """``rebar attach-commits <ticket_id> <sha> [<sha> ...]``."""
    parser = _base(
        prog, "Link commits to a ticket by SHA (repairs a missing rebar-ticket: trailer)."
    )
    parser.add_argument("ticket_id", nargs="?", help="the ticket to attach commits to")
    parser.add_argument("shas", nargs="*", help="one or more commit SHAs")
    return parser


def build_session_log(*, prog: str) -> argparse.ArgumentParser:
    """``rebar session-log <start|append> [<entry>] [--summary=] [--relates-to=]``."""
    parser = _base(
        prog,
        'Capture helper: append "<entry>" to the current session_log '
        "(start rotates to a fresh log).",
    )
    parser.add_argument("--summary", help="summary for a new (start) log")
    parser.add_argument("--relates-to", help="ticket this log relates to")
    parser.add_argument("--discovered-from", help="ticket this log was discovered from")
    parser.add_argument("verb", nargs="?", help="start|append")
    parser.add_argument("entry", nargs="?", help="the log entry text")
    return parser
