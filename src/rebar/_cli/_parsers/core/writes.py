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


def _base(prog: str) -> argparse.ArgumentParser:
    return build_argument_parser(prog=prog, add_help=False, allow_abbrev=False)


def build_create(*, prog: str) -> argparse.ArgumentParser:
    """``rebar create <type> <title> [<parent>] [flags] [--output json]``."""
    parser = _base(prog)
    parser.add_argument("--output", "-o", choices=("text", "json"), default="text")
    parser.add_argument("--parent")
    parser.add_argument("--priority", "-p")
    parser.add_argument("--description", "-d")
    parser.add_argument("--assignee")
    parser.add_argument("--tags")
    parser.add_argument("--bridge-project")
    parser.add_argument("--repos")
    parser.add_argument("--detected-by")
    parser.add_argument("ticket_type", nargs="?")
    parser.add_argument("title", nargs="?")
    parser.add_argument("parent", nargs="?")
    return parser


def build_idea(*, prog: str) -> argparse.ArgumentParser:
    """``rebar idea <title> [--description=<text>] [--output json]``."""
    parser = _base(prog)
    parser.add_argument("--output", "-o", choices=("text", "json"), default="text")
    parser.add_argument("--description", "-d")
    parser.add_argument("title", nargs="?")
    return parser


def build_comment(*, prog: str) -> argparse.ArgumentParser:
    """``rebar comment <ticket_id> <body>`` (positionals only)."""
    parser = _base(prog)
    parser.add_argument("ticket_id", nargs="?")
    parser.add_argument("body", nargs="?")
    return parser


def build_link(*, prog: str) -> argparse.ArgumentParser:
    """``rebar link <id1> <id2> <relation> [--dry-run]``."""
    parser = _base(prog)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("source", nargs="?")
    parser.add_argument("target", nargs="?")
    parser.add_argument("relation", nargs="?")
    return parser


def build_unlink(*, prog: str) -> argparse.ArgumentParser:
    """``rebar unlink <id1> <id2> [<relation>]``."""
    parser = _base(prog)
    parser.add_argument("source", nargs="?")
    parser.add_argument("target", nargs="?")
    parser.add_argument("relation", nargs="?")
    return parser


def build_revert(*, prog: str) -> argparse.ArgumentParser:
    """``rebar revert <ticket_id> <target_uuid> [--reason=<text>]``."""
    parser = _base(prog)
    parser.add_argument("--reason", default="")
    parser.add_argument("ticket_id", nargs="?")
    parser.add_argument("target_uuid", nargs="?")
    return parser


def build_edit(*, prog: str) -> argparse.ArgumentParser:
    """``rebar edit <ticket_id> [--<field>=VALUE ...] [tag deltas] [--review]``."""
    parser = _base(prog)
    parser.add_argument("--title")
    parser.add_argument("--priority")
    parser.add_argument("--assignee")
    parser.add_argument("--ticket_type")
    parser.add_argument("--description")
    parser.add_argument("--parent")
    parser.add_argument("--bridge-project")
    parser.add_argument("--add-tag")
    parser.add_argument("--remove-tag")
    parser.add_argument("--set-tags")
    parser.add_argument("--review", action="store_true")
    parser.add_argument("ticket_id", nargs="?")
    return parser


def build_tag(*, prog: str) -> argparse.ArgumentParser:
    """``rebar tag <ticket_id> <tag>`` (positionals only)."""
    parser = _base(prog)
    parser.add_argument("ticket_id", nargs="?")
    parser.add_argument("tag", nargs="?")
    return parser


def build_untag(*, prog: str) -> argparse.ArgumentParser:
    """``rebar untag <ticket_id> <tag>`` (positionals only)."""
    parser = _base(prog)
    parser.add_argument("ticket_id", nargs="?")
    parser.add_argument("tag", nargs="?")
    return parser


def build_archive(*, prog: str) -> argparse.ArgumentParser:
    """``rebar archive <ticket_id>`` (single positional)."""
    parser = _base(prog)
    parser.add_argument("ticket_id", nargs="?")
    return parser


def build_set_file_impact(*, prog: str) -> argparse.ArgumentParser:
    """``rebar set-file-impact <id> <json_array> | <id> --none <reason>``."""
    parser = _base(prog)
    parser.add_argument("--none", dest="none_reason", nargs="?", const="")
    parser.add_argument("ticket_id", nargs="?")
    parser.add_argument("value", nargs="?")
    return parser


def build_set_verify_commands(*, prog: str) -> argparse.ArgumentParser:
    """``rebar set-verify-commands <ticket_id> <json_array>`` (positionals only)."""
    parser = _base(prog)
    parser.add_argument("ticket_id", nargs="?")
    parser.add_argument("json_array", nargs="?")
    return parser


def build_attach_commits(*, prog: str) -> argparse.ArgumentParser:
    """``rebar attach-commits <ticket_id> <sha> [<sha> ...]``."""
    parser = _base(prog)
    parser.add_argument("ticket_id", nargs="?")
    parser.add_argument("shas", nargs="*")
    return parser


def build_session_log(*, prog: str) -> argparse.ArgumentParser:
    """``rebar session-log <start|append> [<entry>] [--summary=] [--relates-to=]``."""
    parser = _base(prog)
    parser.add_argument("--summary")
    parser.add_argument("--relates-to")
    parser.add_argument("--discovered-from")
    parser.add_argument("verb", nargs="?")
    parser.add_argument("entry", nargs="?")
    return parser
