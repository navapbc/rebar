"""Lean CLI parser factories (RP-05 S2c).

Each factory builds a command family's :class:`~rebar._cli._parser.RebarArgumentParser`
from the S2a factory foundation, importing ONLY the standard library and
:mod:`rebar._cli._parser`. Heavy optional runtime (LLM providers, the UI web stack,
the reconciler engine, Jira transport, botocore, …) stays deferred inside the
command handlers, never pulled in by parser construction.
"""

from __future__ import annotations
