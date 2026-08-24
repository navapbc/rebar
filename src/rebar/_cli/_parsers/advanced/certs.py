"""Certificate/environment maintenance parser factories (RP-05 S2c).

Prog-bound factories for ``rebar trusted-env`` (from
:mod:`rebar._commands.trusted_env_cmd`) and ``rebar remote-cert`` (from
:mod:`rebar._commands.remote_cert`). Only the stdlib and
:mod:`rebar._cli._parser` are imported at module top-level; the handler modules
that own the single-source presentation constants (``_USAGE`` / ``__doc__``) are
imported lazily inside the ``build`` functions.
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


def build_trusted_env(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar trusted-env`` parser (``add`` / ``revoke``) bound to ``prog``.

    ``add_help=False`` is preserved: the handler renders its own usage banner for
    ``--help`` / ``-h`` / ``help`` before argparse ever runs.
    """
    from rebar._commands.trusted_env_cmd import _USAGE

    parser = build_argument_parser(
        prog=prog,
        usage=_USAGE,
        description="Add or revoke trusted environment keys with ticket log position anchors.",
        add_help=False,
        formatter_class=argparse.HelpFormatter,
    )
    parser.add_argument("verb", choices=("add", "revoke"))
    parser.add_argument("env_id")
    parser.add_argument("target", help="<public_key> for add, or <public_key-or-index> for revoke")
    parser.add_argument("--root", help="repo root that resolves the ticket store (default: cwd)")
    return parser


def build_remote_cert(*, prog: str) -> argparse.ArgumentParser:
    """Build the ``rebar remote-cert`` parser bound to ``prog``."""
    from rebar._commands import remote_cert

    parser = build_argument_parser(
        prog=prog,
        usage=remote_cert._USAGE,
        description=remote_cert.__doc__,
        formatter_class=argparse.HelpFormatter,
    )
    parser.add_argument("ticket_id", help="the ticket to certify")
    parser.add_argument("kind", choices=remote_cert._VALID_KINDS, help="the gate kind to run")
    parser.add_argument("--root", help="repo root (default: cwd)")
    return parser
