"""``rebar reclaim-collapse`` parser factory.

The command is an offline S1 dry-run/apply surface over an explicitly marked shadow
tracker. It has no publish/swap options.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def build_reclaim_collapse(*, prog: str) -> argparse.ArgumentParser:
    """``rebar reclaim-collapse --shadow-tracker PATH --boundary SHA [--apply]``."""
    parser = build_argument_parser(
        prog=prog,
        description="Build an offline below-horizon tickets history collapse in a shadow clone.",
        usage=(
            "%(prog)s --shadow-tracker SHADOW_TRACKER --boundary\n"
            "                              BOUNDARY [--branch BRANCH] [--apply]\n"
            "                              [--format {text,json}]"
        ),
        add_help=False,
        allow_abbrev=False,
    )
    parser.add_argument(
        "--shadow-tracker",
        required=True,
        help="path to an explicitly marked disposable tickets-branch shadow clone",
    )
    parser.add_argument(
        "--boundary",
        required=True,
        help="oldest retained boundary commit to collapse into the checkpoint",
    )
    parser.add_argument(
        "--branch",
        default="HEAD",
        help="shadow branch/ref tip to rewrite (default: HEAD)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="update the shadow clone's current branch; omitted means dry-run only",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format",
    )
    return parser
