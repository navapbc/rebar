"""``rebar bridge`` parser factory (RP-05 S2c).

Reproduces the nested ``rebar bridge`` grammar (preview / sync / run / status /
pause / resume / fsck / check-access / setup / projects, with ``projects``' own
list/set/remove verbs) from :mod:`rebar._cli._bridge_commands`, bound to a
caller-supplied ``prog``. Uses argparse's default help formatter (as the inline
parser did) so help renders byte-for-byte unchanged.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import RebarHelpFormatter, build_argument_parser


def _positive_int(value: str) -> int:
    """Argparse converter for canonical mutation ceilings."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _duration_seconds(value: str) -> int:
    """Parse a positive integer duration with an optional s/m/h suffix."""
    units = {"s": 1, "m": 60, "h": 3600}
    suffix = value[-1:].lower()
    number = value[:-1] if suffix in units else value
    try:
        parsed = int(number)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive duration (for example 2h)") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive duration (for example 2h)")
    return parsed * units.get(suffix, 1)


def build(*, prog: str) -> argparse.ArgumentParser:
    """Create the parser for the supported bridge operations, bound to ``prog``."""
    parser = build_argument_parser(
        prog=prog,
        description="Synchronize rebar tickets with Jira.",
        formatter_class=RebarHelpFormatter,
    )
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        title="commands",
        metavar="{preview,run,sync,status,pause,resume,fsck,check-access,setup,projects}",
    )
    preview = commands.add_parser(
        "preview",
        help="Show proposed Jira changes without applying them.",
        description="Show proposed Jira changes without applying them.",
    )
    sync = commands.add_parser(
        "sync",
        help="Apply proposed Jira changes.",
        description="Apply proposed Jira changes.",
    )
    run = commands.add_parser(
        "run",
        help="Run one scheduled bridge profile and strictly deliver ticket events.",
        description="Run one scheduled bridge profile and strictly deliver ticket events.",
    )
    run.add_argument(
        "--profile",
        metavar="PROFILE",
        help="Scheduled compatibility profile; defaults to the provider configuration.",
    )
    for command in (preview, sync):
        selection = command.add_mutually_exclusive_group()
        selection.add_argument(
            "--only", metavar="IDS", help="Examine only these local IDs or bound Jira keys."
        )
        selection.add_argument(
            "--except",
            dest="except_ids",
            metavar="IDS",
            help="Exclude these local IDs or bound Jira keys.",
        )
    sync.add_argument(
        "--max-changes",
        type=_positive_int,
        metavar="N",
        help="Apply at most N proposed changes and retain an audit manifest.",
    )
    status = commands.add_parser(
        "status",
        help="Show the reconciler's durable status snapshot.",
        description="Show the reconciler's durable status snapshot.",
    )
    status.add_argument("--target", metavar="ENVIRONMENT_ID")
    status.add_argument("--max-age", type=_duration_seconds, metavar="DURATION")
    status.add_argument("--json", action="store_true", help="Emit the snapshot as JSON.")
    pause = commands.add_parser(
        "pause",
        help="Temporarily stop scheduled reconciliation.",
        description="Temporarily stop scheduled reconciliation.",
    )
    pause.add_argument("reason", metavar="REASON")
    commands.add_parser(
        "resume",
        help="Resume scheduled reconciliation.",
        description="Resume scheduled reconciliation.",
    )
    fsck = commands.add_parser(
        "fsck",
        add_help=False,
        help="Audit committed event compatibility and binding-store integrity.",
    )
    fsck.add_argument("args", nargs=argparse.REMAINDER)
    check_access = commands.add_parser(
        "check-access",
        add_help=False,
        help="Check live Jira access with a create/search/delete round-trip.",
    )
    check_access.add_argument("args", nargs=argparse.REMAINDER)
    setup = commands.add_parser(
        "setup",
        add_help=False,
        help="Interactively configure and validate Jira access.",
    )
    setup.add_argument("args", nargs=argparse.REMAINDER)
    suggest_mapping = commands.add_parser(
        "suggest-mapping",
        add_help=False,
        help="Probe a live Jira project (read-only) and suggest a [mapping] config block.",
    )
    suggest_mapping.add_argument("args", nargs=argparse.REMAINDER)
    projects = commands.add_parser(
        "projects",
        help="Manage the store's bridge-projects sync mapping.",
        description="List, set, or remove the store's bridge-projects sync mapping.",
    )
    project_verbs = projects.add_subparsers(
        dest="projects_verb",
        required=True,
        title="projects commands",
        metavar="{list,set,remove}",
    )
    project_verbs.add_parser(
        "list",
        help="Print the projects mapping as JSON.",
        description="Print the projects mapping as JSON.",
    )
    projects_set = project_verbs.add_parser(
        "set",
        help="Set a project key's repos (replace semantics).",
        description="Set a project key's repos (replace semantics).",
    )
    projects_set.add_argument("key", metavar="KEY")
    projects_set.add_argument(
        "--repos",
        required=True,
        metavar="REPOS",
        help="Comma-separated repo list; an empty string stores no repos.",
    )
    projects_remove = project_verbs.add_parser(
        "remove",
        help="Remove a project key from the mapping.",
        description="Remove a project key from the mapping.",
    )
    projects_remove.add_argument("key", metavar="KEY")
    return parser
