"""``rebar bridge setup`` / ``rebar jira-onboard`` parser factory (RP-05 S2c).

The interactive Jira onboarding wizard is reachable under two program names that
differ ONLY by ``prog``: the primary ``rebar bridge setup`` and its retained
compatibility alias ``rebar jira-onboard``. Both share ONE argument-definition
function
(:func:`_define`) via :func:`rebar._cli._parser.compose` (AC2), so their option
surfaces cannot drift. Only the stdlib and :mod:`rebar._cli._parser` are imported
at module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import compose

_DESCRIPTION = (
    "Interactively configure Jira: detect existing settings, prompt for "
    "missing url/user/project, persist them to rebar.toml, and validate via "
    "bridge check-access. The secret JIRA_API_TOKEN stays an environment variable "
    "and is never written to a config file."
)


def _define(parser: argparse.ArgumentParser) -> None:
    """Add the shared onboarding option surface to ``parser``."""
    parser.add_argument("--url", help="Jira base URL (non-interactive)")
    parser.add_argument("--user", help="Jira account email (non-interactive)")
    parser.add_argument("--project", help="default Jira project key (non-interactive)")
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="skip the post-setup bridge check-access check",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="clear the persisted [jira] url/user/project and exit (no re-prompt)",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true", help="skip the --reset confirmation prompt"
    )


def build(*, prog: str) -> argparse.ArgumentParser:
    """Build the Jira onboarding parser bound to ``prog``."""
    return compose(
        _define,
        prog=prog,
        description=_DESCRIPTION,
        formatter_class=argparse.HelpFormatter,
    )
