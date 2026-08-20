"""``rebar sign`` / ``verify-signature`` parser factories (RP-05 S2b).

Prog-bound argparse renderings of the two attestation arms in
:mod:`rebar.signing`. ``sign`` takes ``<ticket_id> <manifest_json>``; both honour
the ``report`` ``--output`` profile, and ``verify-signature`` accepts an optional
``--kind`` selector. The handlers keep their bespoke ``Usage:`` diagnostics,
``SigningError`` exit codes, and verdict rendering. Only the stdlib and
:mod:`rebar._cli._parser` are imported at module top-level.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def build_sign(*, prog: str) -> argparse.ArgumentParser:
    """``rebar sign <ticket_id> <manifest_json> [--output json]``."""
    parser = build_argument_parser(prog=prog, add_help=False, allow_abbrev=False)
    parser.add_argument("--output", "-o", choices=("text", "json"), default="text")
    parser.add_argument("ticket_id", nargs="?")
    parser.add_argument("manifest_json", nargs="?")
    return parser


def build_verify_signature(*, prog: str) -> argparse.ArgumentParser:
    """``rebar verify-signature <ticket_id> [--kind <kind>] [--output json]``."""
    parser = build_argument_parser(prog=prog, add_help=False, allow_abbrev=False)
    parser.add_argument("--output", "-o", choices=("text", "json"), default="text")
    parser.add_argument("--kind")
    parser.add_argument("ticket_id", nargs="?")
    return parser
