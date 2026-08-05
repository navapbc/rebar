"""Language-agnostic analyzer contract for code-health metrics.

The concrete adapters in :mod:`rebar.metrics.analyzers` (``scc_loc``,
``lizard_complexity``, ``jscpd_dup``) are composed directly by
:mod:`rebar.metrics.git_metrics`; this module supplies the shared result shape
and the structural protocol they conform to. It deliberately holds no registry
or dispatch — per-language analyzer *selection* is not a shipped capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from rebar.metrics.registry import Unavailable


@dataclass(frozen=True)
class AnalyzerResult:
    """Signals produced by a configured code-health analyzer."""

    loc: Any | None = None
    complexity: Any | None = None
    duplication: Any | None = None


class Analyzer(Protocol):
    """A code-health analyzer selected for one or more languages."""

    def analyze(
        self,
        repo_root: Path,
        languages: tuple[str, ...] | None = None,
    ) -> AnalyzerResult | Unavailable: ...
