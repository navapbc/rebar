"""rebar code-grounding evidence oracle (epic 8f6c).

A pure EVIDENCE oracle: it returns evidence (outcome + structured reason +
provenance/coverage) and NEVER decides block/advisory — that policy lives in the
consuming code. Every backend FAILS OPEN: unsupported language / missing tool /
crash / timeout / version-skew becomes a recorded ``abstain``, never a false
accusation.

This package is the dependency root (story 0b2b): the three-valued evidence
**contract** (:mod:`.evidence`), the SARIF interchange at the edges
(:mod:`.sarif`), and the fail-open execution **harness** (:mod:`.harness`). The
backends (Engine A refutation, T0 dependency existence, Engine B detectors) build
ON this and are added by later stories.

The contract + harness are stdlib-only and import-clean, so importing
:mod:`rebar.grounding` pulls NO heavy stack. The optional ``grounding`` extra adds
the in-process binding (tree-sitter) used by later backends; those imports are
guarded via :func:`rebar._optional.guard_import` and a non-adopting client pays
nothing.
"""

from __future__ import annotations

from . import deps, engine_b, evidence, harness, oracle, resolve, sarif, semantic
from .deps import enumerate_dependencies, refute_package, refute_packages
from .engine_b import ScanResult
from .evidence import (
    ABSTAIN_REASONS,
    JOBS,
    OUTCOMES,
    TIERS,
    GroundingContractError,
    abstain,
    coverage,
    is_resolved,
    match,
    normalize_evidence,
    refuted,
)
from .harness import RunResult, run_in_worker, run_tool
from .oracle import (  # the unified public facade (S5)
    DIMENSIONS,
    DIMENSIONS_VERSION,
    applies,
    contract,
    is_known_dimension,
    refute_absence,
    scan,
)
from .resolve import (
    REFERENCE_KINDS,
    extract_references,
    extract_references_from_diff,
    validate_reference,
)

__all__ = [
    # evidence contract
    "ABSTAIN_REASONS",
    "DIMENSIONS",
    "DIMENSIONS_VERSION",
    "JOBS",
    "OUTCOMES",
    "REFERENCE_KINDS",
    "TIERS",
    "GroundingContractError",
    # fail-open harness
    "RunResult",
    # Engine B — detector scan (S4)
    "ScanResult",
    "abstain",
    "applies",
    "contract",
    "coverage",
    "deps",
    "engine_b",
    "enumerate_dependencies",
    "evidence",
    "extract_references",
    "extract_references_from_diff",
    "harness",
    "is_known_dimension",
    "is_resolved",
    "match",
    "normalize_evidence",
    "oracle",
    # Public oracle facade — the three query surfaces (S5)
    "refute_absence",
    # T0 — dependency existence (S3)
    "refute_package",
    "refute_packages",
    "refuted",
    "resolve",
    "run_in_worker",
    "run_tool",
    "sarif",
    "scan",
    "semantic",
    # Engine A — refutation resolver (S2)
    "validate_reference",
]
