"""Isolated, optional adapters for external metric sources (ticket 1f77).

Adapters in this package parse metrics from external systems (e.g. GitHub
Actions) and are imported *only* by their harvest scripts (and tests) — never
by the core ``rebar.metrics`` package or its registry. Importing an adapter
registers nothing into ``REGISTRY`` and changes no core behavior.
"""

from __future__ import annotations
