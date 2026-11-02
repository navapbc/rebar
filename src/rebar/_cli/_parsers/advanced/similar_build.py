"""``rebar store build-vectors`` parser factory."""

from __future__ import annotations

import argparse

from rebar._cli._parser import RebarHelpFormatter, build_argument_parser


def build(*, prog: str) -> argparse.ArgumentParser:
    """Build the opt-in similar-ticket vector-build parser."""

    parser = build_argument_parser(
        prog=prog,
        description="Digest and embed indexed tickets for the opt-in similar-ticket tier.",
        epilog=(
            "The build is explicit: no other command calls it, so ticket creation never "
            "pays for a provider call. It is incremental and resumable -- each ticket is "
            "committed as it is embedded, so an interrupted run keeps what it reached."
        ),
        formatter_class=RebarHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="embed at most N tickets this run, leaving the rest for a later run",
    )
    parser.add_argument(
        "--output",
        choices=("text", "json"),
        default="text",
        help="output format (default: text)",
    )
    return parser
