"""Code-review capability package (epic b744) — the four-pass code-review gate.

Off by default and source-separated: nothing here runs unless `verify.enable_code_review` is
on. The pieces: the diff context-assembler (:mod:`assemble`), the overlay-id registry +
criteria routing (:mod:`registry`), the move-catalog (:mod:`moves`), the structured-output
contracts (:mod:`contracts`), the escalation/Pass-wiring scripted ops (:mod:`workflow_ops`) +
the per-overlay :mod:`batch_runner`, the verdict sidecar (:mod:`sidecar`), and the public
gate-backed surface (:mod:`shim`).

The SINGLE-PASS route is RETIRED (WS4, ADR 0011): ``review_code`` is now the gate-backed shim —
it keeps its name/signature and ``review_result`` return shape, but its implementation is the
four-pass gate (inert empty result when disabled). Importing this package registers the
structured-output contracts (cheap — pydantic is lazy).
"""

from __future__ import annotations

# Register the structured-output contracts on import (cheap — pydantic lazy in the builders).
from rebar.llm.code_review import contracts as _contracts  # noqa: F401

# The public surface — gate-backed (replaces the retired single-pass review_code).
from rebar.llm.code_review.shim import review_code

__all__ = ["review_code"]
