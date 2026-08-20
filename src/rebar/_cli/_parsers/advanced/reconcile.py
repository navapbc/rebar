"""``rebar reconcile`` parser factory (RP-05 S2c).

Reproduces the reconciler request grammar (``preview``/``sync`` plus the retained
legacy ``--mode`` adapter and selection flags) from
:mod:`rebar._engine.rebar_reconciler.request`, bound to a caller-supplied ``prog``.

The reconciler itself runs in a subprocess (``python -m rebar_reconciler``), whose
own in-process parser stays the authoritative argv gate; this lean factory is the
import-clean census/registry mirror — it never imports the engine package.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def build(*, prog: str) -> argparse.ArgumentParser:
    """Build the reconciler request parser bound to ``prog`` (``allow_abbrev=False``)."""
    parser = build_argument_parser(
        prog=prog,
        allow_abbrev=False,
        formatter_class=argparse.HelpFormatter,
    )
    parser.add_argument("command", nargs="?", choices=("preview", "sync"))
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repository root (default: auto-detect from script location)",
    )
    parser.add_argument(
        "--mode",
        default=None,
        help=(
            "Compatibility mode: reconcile-check | dry-run | bootstrap-strict | "
            "bootstrap-throttle | live (legacy default: live)"
        ),
    )
    parser.add_argument(
        "--dry-run-enumerate",
        action="store_true",
        help="List enumerable tracker directories and exit without running a pass.",
    )
    parser.add_argument(
        "--filter-local-ids",
        default=None,
        help="Compatibility write filter applied after the full differ computation.",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--only", metavar="IDS")
    selection.add_argument("--except", dest="except_ids", metavar="IDS")
    parser.add_argument("--max-changes", type=_positive_int, metavar="N")
    return parser
