"""``rebar transition`` / ``reopen`` / ``claim`` parser factories (RP-05 S2b).

Prog-bound argparse renderings of the three lifecycle arms in
:mod:`rebar._commands.transition` and :mod:`rebar._commands.claim`. All honour the
``report`` ``--output`` profile. ``transition`` models the ``<id> [<current>]
<target>`` positionals plus its close/force flag surface; ``reopen`` takes just an
id; ``claim`` takes an id plus ``--assignee`` / ``--force`` / ``--review``.

The handlers keep their bespoke parsing intact — in particular ``transition`` and
``claim`` retain their hand-rolled inline-only ``--force[=<reason>]`` extraction
(argparse cannot express "optional value that never swallows the next token") and
their exact ``Usage:`` diagnostics and exit codes. Only the stdlib and
:mod:`rebar._cli._parser` are imported at module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def _base(prog: str, description: str | None = None) -> argparse.ArgumentParser:
    return build_argument_parser(
        prog=prog, description=description, add_help=False, allow_abbrev=False
    )


def build_transition(*, prog: str) -> argparse.ArgumentParser:
    """``rebar transition <id> [<current>] <target> [flags]``."""
    parser = _base(prog, "Transition ticket status (optimistic concurrency).")
    parser.add_argument(
        "--output", "-o", choices=("text", "json"), default="text", help="output format"
    )
    parser.add_argument(
        "--reason",
        default="",
        help=(
            "close reason for a reason-required --class "
            "(obsolete/wontfix/not_a_bug/escalated); recorded as close_reason"
        ),
    )
    parser.add_argument("--class", dest="close_class", default="", help="close disposition class")
    parser.add_argument(
        "--caused-by", dest="caused_by", default="", help="ticket that caused this bug"
    )
    parser.add_argument(
        "--ref", default="", help="verify/sign the completion gate against this ref"
    )
    # Inline-only escape hatch; runtime keeps its own hand-rolled extraction.
    parser.add_argument(
        "--force", nargs="?", const="", help="bypass a start/close gate (--force=<reason>)"
    )
    parser.add_argument("ticket_id", nargs="?", help="the ticket to transition")
    parser.add_argument("statuses", nargs="*", help="[<current>] <target> status")
    return parser


def build_reopen(*, prog: str) -> argparse.ArgumentParser:
    """``rebar reopen <ticket_id>``."""
    parser = _base(
        prog, "Reopen a closed ticket (closed -> open; exit 10 if not currently closed)."
    )
    parser.add_argument(
        "--output", "-o", choices=("text", "json"), default="text", help="output format"
    )
    parser.add_argument("ticket_id", nargs="?", help="the ticket to reopen")
    return parser


def build_claim(*, prog: str) -> argparse.ArgumentParser:
    """``rebar claim <ticket_id> [--assignee=<name>] [--force[=<reason>]] [--review]``."""
    parser = _base(
        prog, "Atomically claim an open ticket (-> in_progress + assignee; exit 10 if taken)."
    )
    parser.add_argument(
        "--output", "-o", choices=("text", "json"), default="text", help="output format"
    )
    parser.add_argument(
        "--assignee", default=None, help="override the default assignee (Jira-resolvable)"
    )
    parser.add_argument(
        "--force", nargs="?", const="", help="bypass the start-work gate (--force=<reason>)"
    )
    parser.add_argument("--review", action="store_true", help="emit the review payload")
    parser.add_argument("ticket_id", nargs="?", help="the ticket to claim")
    parser.epilog = "--review: preview payload, not atomic; see review-plan --status."
    return parser
