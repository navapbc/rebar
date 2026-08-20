"""Bridge-arm parser factories (RP-05 S2c, census).

Prog-bound factories for the individually-routed bridge arms ``bridge-fsck``
(reproducing the offline-audit parser in
:func:`rebar._engine_support.bridge_fsck.main`) and ``bridge-probe`` (a live Jira
capability preflight that forwards its argv verbatim to a subprocess probe, so it
carries no options of its own). ``bridge-status`` shares the full ``bridge``
grammar and is wired directly to
:func:`rebar._cli._parsers.advanced.bridge.build`.

Only the stdlib and :mod:`rebar._cli._parser` are imported at module top-level —
the heavy ``bridge_fsck`` engine module is never imported here.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def build_fsck(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar bridge-fsck`` offline-audit parser bound to ``prog``.

    Mirrors :func:`rebar._engine_support.bridge_fsck.main`'s argparse surface. The
    canonical ``--output``/``-o`` flag is consumed by ``parse_output`` before argparse
    runs, so it is intentionally absent here.
    """
    parser = build_argument_parser(
        prog=prog,
        description="Audit committed bridge events and binding-store integrity offline.",
        formatter_class=argparse.HelpFormatter,
    )
    parser.add_argument(
        "--tickets-tracker",
        default=None,
        help=(
            "Path to the .tickets-tracker directory. "
            "Defaults to the REBAR_TRACKER_DIR env var "
            "or <repo-root>/.tickets-tracker."
        ),
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help=(
            "Prune reverse bindings that have no forward entry "
            "(store_integrity / reverse_missing_forward). Refuses if any other "
            "integrity kind is present. This is the only writing mode; the audit "
            "itself never writes."
        ),
    )
    parser.add_argument(
        "--live-visibility",
        action="store_true",
        help=(
            "Opt-in: additionally run a READ-ONLY, ADVISORY live check that the mapped "
            "project keys + legacy_default are visible to the bridge bot, reusing the "
            "reconcile-pass visibility helper. Requires live Jira credentials "
            "(JIRA_URL / JIRA_USER / JIRA_API_TOKEN); when absent it skips cleanly. The "
            "advisory is written to stderr and never changes the exit code."
        ),
    )
    return parser


def build_probe(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar bridge-probe`` parser bound to ``prog``.

    The probe forwards its argv verbatim to the out-of-process Jira capability
    probe and defines no options of its own, so this census parser only carries the
    passthrough tail (and the standard ``--help``).
    """
    parser = build_argument_parser(
        prog=prog,
        description=(
            "Live Jira capability preflight (requires JIRA_URL, JIRA_USER, "
            "JIRA_API_TOKEN; optional JIRA_PROJECT). Creates and deletes a throwaway issue."
        ),
        formatter_class=argparse.HelpFormatter,
    )
    parser.add_argument(
        "probe_args",
        nargs=argparse.REMAINDER,
        help="arguments forwarded verbatim to the capability probe",
    )
    return parser
