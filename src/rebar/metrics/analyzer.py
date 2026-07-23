"""Language-agnostic analyzer contract for code-health metrics."""

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


ANALYZERS: dict[str, Analyzer] = {}


def resolve_analyzer(language: str) -> Analyzer | None:
    """Resolve the analyzer configured for ``language``."""

    return ANALYZERS.get(language)


def analyze_or_unavailable(
    language: str,
    repo_root: Path,
    *,
    languages: tuple[str, ...] | None = None,
    accruing_since: str,
) -> AnalyzerResult | Unavailable:
    """Analyze a repository or report that no analyzer is configured."""

    analyzer = resolve_analyzer(language)
    if analyzer is None:
        return Unavailable(
            reason=f"no analyzer configured for {language}",
            accruing_since=accruing_since,
        )
    return analyzer.analyze(repo_root, languages=languages)
