"""Core-command parser factories (RP-05 S2b).

One lean module per ``rebar`` *core* command family (the counterpart to the
sibling ``advanced`` package built in S2c). Every factory exposes a
``build(*, prog: str)`` — or ``build_<name>(*, prog)`` where a module hosts
several flat commands — that returns a prog-bound
:class:`~rebar._cli._parser.RebarArgumentParser` modelling the family's currently
ACCEPTED argument grammar (positionals, options, ``usage=``, ``add_help``,
``allow_abbrev``), importing only the stdlib and :mod:`rebar._cli._parser`.

These factories are a side-effect-free, import-isolated census of the grammar;
the runtime handlers keep their bespoke argv parsing and byte-exact diagnostics
(family-specific unknown-token wording, exit codes, help text), exactly as the
``metrics``/``audit`` advanced factories do. So a factory models what a command
ACCEPTS and how it renders help/usage — never argparse's default error text.
"""

from __future__ import annotations
