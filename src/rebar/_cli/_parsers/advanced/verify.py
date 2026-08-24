"""Verify-gate parser factories (RP-05 S2c).

Prog-bound factories for the merge-gate verbs ``verify-opcert`` /
``verify-commit-ticket`` / ``verify-identity`` (a.k.a. ``verify-authorship``),
reproducing the parsers from :mod:`rebar._commands.verify_opcert`,
:mod:`rebar._commands.verify_commit`, and :mod:`rebar._commands.verify_authorship`.

Each carries a custom ``usage=`` and a ``RawDescriptionHelpFormatter`` (preserved
exactly). The shared presentation constants live here and are imported by the
handlers, keeping a single source with no drift. Only the stdlib and
:mod:`rebar._cli._parser` are imported at module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser

OPCERT_USAGE = (
    "rebar verify-opcert [--require-environment <env_id>] [--since <ref>] "
    "[--format {text,json}] [--root <path>]"
)

COMMIT_USAGE = "rebar verify-commit-ticket [--rev <ref> | --message-file <path> | --message <text>]"

IDENTITY_USAGE = (
    "rebar verify-identity [--all | --base <ref>] [--require-authenticated] "
    "[--since <ref>] [--format {text,json}] [--root <path>]"
)


def build_opcert(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar verify-opcert`` parser bound to ``prog``."""
    parser = build_argument_parser(
        prog=prog,
        usage=OPCERT_USAGE,
        description=(
            "Verify the required-environment operation certificate of the store's closed "
            "tickets, which is the op-cert merge-gate. It walks the merged log, groups events "
            "by ticket, and for each in-scope closed ticket verifies that "
            "verify.require_environment (or --require-environment) produced a valid "
            "completion-verifier op-cert against its out-of-band-pinned key in "
            ".rebar/trusted_environments.yaml. It is advisory unless a required environment is "
            "set, in which case any enforced closed ticket without a valid cert fails the gate "
            "with a non-zero exit. Tickets whose close commit predates --since or "
            "verify.opcert_enforce_since are grandfathered, which means they are reported but "
            "never fail the gate."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--require-environment",
        metavar="ENV_ID",
        help="environment that must sign (default: verify.require_environment)",
    )
    parser.add_argument(
        "--since",
        help="grandfather boundary. Only enforce tickets closed at or descending this ref "
        "(default: verify.opcert_enforce_since)",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format (default: text). json prints only a report array to stdout",
    )
    parser.add_argument("--root", help="repo root that resolves the ticket store (default: cwd)")
    return parser


def build_commit_ticket(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar verify-commit-ticket`` parser bound to ``prog``."""
    from rebar._commands.verify_commit import EXPECTED_FORMAT

    parser = build_argument_parser(
        prog=prog,
        usage=COMMIT_USAGE,
        description=(
            "Verify a commit message references a rebar ticket that resolves in the store.\n\n"
            + EXPECTED_FORMAT
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--rev", help="git revision to read the message from (default: HEAD)")
    src.add_argument("--message-file", help="read the commit message from this file")
    src.add_argument("--message", help="the commit message text (inline)")
    parser.add_argument("--root", help="repo root that resolves the ticket store (default: cwd)")
    return parser


def build_identity(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar verify-identity`` / ``verify-authorship`` parser bound to ``prog``."""
    parser = build_argument_parser(
        prog=prog,
        usage=IDENTITY_USAGE,
        description=(
            "Verify authenticated authorship of the store's mutating events against each "
            "author identity's epoch-scoped keyring. This is the authorship merge-gate, also "
            "available under the back-compat alias `rebar verify-authorship`. It is advisory "
            "unless identity.require_authenticated (or --require-authenticated) is on, in which "
            "case any enforced event that is not `verified` fails the gate with a non-zero "
            "exit. Events whose introducing commit predates --since or identity.enforce_since "
            "are grandfathered, which means they are reported but never fail the gate."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true", help="scan the whole store (default)")
    scope.add_argument("--base", help="only events changed in <base>..HEAD on the tracker branch")
    parser.add_argument(
        "--require-authenticated",
        action="store_true",
        help="force enforcement on, regardless of the identity.require_authenticated config",
    )
    parser.add_argument(
        "--since",
        help="grandfather boundary. Only enforce events at or descending this ref "
        "(default: identity.enforce_since)",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format (default: text). json prints only a report array to stdout",
    )
    parser.add_argument("--root", help="repo root that resolves the ticket store (default: cwd)")
    return parser
