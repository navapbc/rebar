"""Certificate parser factory (RP-05 S2c).

The prog-bound factory for ``rebar remote-cert`` (from
:mod:`rebar._commands.remote_cert`). Only the stdlib and
:mod:`rebar._cli._parser` are imported at module top-level; the handler module
that owns the single-source presentation constants (``_USAGE`` / ``__doc__``) is
imported lazily inside the ``build`` function. ``rebar trusted-env`` was removed
with the repository op-cert pin file (ADR 0125).
"""

from __future__ import annotations

import argparse

from rebar._cli._parser import build_argument_parser


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
