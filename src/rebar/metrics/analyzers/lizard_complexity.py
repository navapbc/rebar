"""Cyclomatic-complexity analysis backed by the optional ``lizard`` library."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rebar._optional import OptionalDependencyError, guard_import
from rebar.metrics.analyzer import AnalyzerResult
from rebar.metrics.registry import Unavailable

_ACCRUING_SINCE = "2026-01-01T00:00:00+00:00"


def analyze(
    repo_root: Path,
    languages: tuple[str, ...] | None = None,
) -> AnalyzerResult | Unavailable:
    """Return deterministic, per-function cyclomatic complexity for ``repo_root``.

    Lizard detects its supported source languages from the files it scans.  The
    optional ``languages`` argument keeps this adapter compatible with the
    common analyzer protocol without narrowing Lizard's native coverage.
    """

    del languages
    try:
        lizard = guard_import("lizard", extra="metrics")
    except OptionalDependencyError as exc:
        return Unavailable(reason=str(exc), accruing_since=_ACCRUING_SINCE)

    root = repo_root.resolve()
    files = _file_complexity(lizard.analyze([str(root)]), root)
    return AnalyzerResult(
        complexity={
            "files": files,
            "functions": sum(len(file["functions"]) for file in files.values()),
            "total_ccn": sum(file["total_ccn"] for file in files.values()),
            "max_ccn": max((file["max_ccn"] for file in files.values()), default=0),
        }
    )


def _file_complexity(analysis: Any, repo_root: Path) -> dict[str, dict[str, Any]]:
    """Normalize Lizard's file records into a stable, repository-relative shape."""

    files: dict[str, dict[str, Any]] = {}
    for file_info in analysis:
        location = Path(file_info.filename).resolve()
        try:
            relative = location.relative_to(repo_root).as_posix()
        except ValueError as exc:
            raise ValueError(f"lizard reported a file outside repository: {location}") from exc

        functions = sorted(
            (
                {
                    "name": function.name,
                    "ccn": function.cyclomatic_complexity,
                    "start_line": function.start_line,
                }
                for function in file_info.function_list
            ),
            key=lambda function: (function["start_line"], function["name"]),
        )
        files[relative] = {
            "functions": functions,
            "total_ccn": sum(function["ccn"] for function in functions),
            "max_ccn": max((function["ccn"] for function in functions), default=0),
        }
    return dict(sorted(files.items()))
