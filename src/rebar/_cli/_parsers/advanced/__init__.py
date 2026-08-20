"""Advanced-command parser factories (RP-05 S2c).

One lean module per ``rebar`` command family. Every factory exposes a
``build(*, prog: str)`` (or ``build_<cmd>(*, prog)`` where a module hosts several
flat commands) that returns a prog-bound
:class:`~rebar._cli._parser.RebarArgumentParser` reproducing the family's current
argument surface EXACTLY — same arguments, help, descriptions, ``usage=``,
``epilog=``, ``formatter_class``, ``add_help``, and nested subcommands — while
importing only the stdlib and :mod:`rebar._cli._parser`.
"""

from __future__ import annotations
