"""Detector registry for Engine B (story 48d7 / epic 8f6c).

A *detector* is a thin rebar envelope riding on a **verbatim native matcher
payload** (the Trivy model): the file IS a valid opengrep/semgrep rule YAML (or an
ast-grep rule), and rebar's metadata lives in ``metadata.rebar_envelope`` so the
engine preserves it untouched. This subpackage owns *discovery + load + cache +
quarantine* of those files; :mod:`rebar.grounding.engine_b` owns *evaluation*.

Public surface (import from this subpackage, not the package ``__init__``):

* :class:`Detector` — one loaded, parsed detector (id, envelope, backend, source).
* :class:`Registry` — the read-only in-memory snapshot of all detectors.
* :func:`load_registry` — the process-local, mtime-cached registry builder.
* :data:`DIMENSIONS` — a mirror of the canonical closed dimension vocabulary owned
  by :data:`rebar.grounding.oracle.DIMENSIONS` (kept in sync; see :mod:`registry`).
"""

from __future__ import annotations

from .registry import (
    BACKEND_ASTGREP,
    BACKEND_METRIC,
    BACKEND_OPENGREP,
    BACKENDS,
    DIMENSIONS,
    Detector,
    Registry,
    load_registry,
)

__all__ = [
    "BACKEND_ASTGREP",
    "BACKEND_METRIC",
    "BACKEND_OPENGREP",
    "BACKENDS",
    "DIMENSIONS",
    "Detector",
    "Registry",
    "load_registry",
]
