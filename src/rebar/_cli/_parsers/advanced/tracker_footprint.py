"""``rebar tracker-footprint`` parser factory."""

from __future__ import annotations

import argparse

from rebar._cli._parser import RebarHelpFormatter, build_argument_parser


def build(*, prog: str) -> argparse.ArgumentParser:
    """Build the opt-in, read-only tracker-footprint parser."""

    parser = build_argument_parser(
        prog=prog,
        description="Measure Git, checkout, allocation, and whole-clone tracker footprint.",
        formatter_class=RebarHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument(
        "--fresh-clone",
        action="store_true",
        help="measure a temporary unfiltered clone of the configured tickets ref",
    )
    parser.add_argument(
        "--output",
        choices=("text", "json"),
        default="text",
        help="output format (default: text)",
    )
    return parser
