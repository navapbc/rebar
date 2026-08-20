"""``rebar identity`` parser factory (RP-05 S2c, census).

A prog-bound argparse rendering of the ``rebar identity`` grammar owned today by
the hand-rolled parsers in :mod:`rebar._commands.identity`
(``create`` / ``use`` / ``key``). Registered for registry census + the AC3
import-isolation probe; the runtime handler keeps its bespoke argv walk and error
text. Only the stdlib and :mod:`rebar._cli._parser` are imported at module
top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def build(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar identity`` nested parser bound to ``prog``."""
    parser = build_argument_parser(
        prog=prog, formatter_class=argparse.HelpFormatter, allow_abbrev=False
    )
    subs = parser.add_subparsers(dest="verb", metavar="{create,use,key}")

    create = subs.add_parser(
        "create", help="create an identity", add_help=False, allow_abbrev=False
    )
    create.add_argument("--name", help="the identity's display name")
    create.add_argument("--email", help="the identity's email")
    create.add_argument(
        "--mapping",
        action="append",
        default=[],
        metavar="<provider>:<external_id>",
        help="an external-user mapping (repeatable)",
    )
    create.add_argument(
        "--key",
        action="append",
        default=[],
        metavar="<authorized-keys line>",
        help="an authorized-keys public key (repeatable)",
    )
    create.add_argument("--self", action="store_true", help="also point self-identity here")

    use = subs.add_parser(
        "use", help="set the self-identity pointer", add_help=False, allow_abbrev=False
    )
    use.add_argument("id", help="the identity to use")

    key = subs.add_parser(
        "key", help="add or revoke an identity key", add_help=False, allow_abbrev=False
    )
    key.add_argument("action", choices=("add", "revoke"))
    key.add_argument("id", help="the identity to modify")
    key.add_argument("public_key", metavar="pubkey", help="the authorized-keys line")
    key.add_argument("--signature-file", help="DSSE envelope authorizing the change")
    return parser
