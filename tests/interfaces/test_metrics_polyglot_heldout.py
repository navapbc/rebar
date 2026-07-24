"""Held-out fallback contracts for polyglot code-health projects. WITHHELD."""

from __future__ import annotations

from pathlib import Path

import pytest
from metrics_polyglot_support import copy_project, git_only_bin, run_metrics

pytestmark = pytest.mark.interface


@pytest.mark.parametrize(
    ("language", "source_path"),
    [
        ("python", "src/main.py"),
        ("typescript", "src/main.ts"),
    ],
)
def test_metrics_cli_names_missing_analyzers_without_fabricating_zeroes(
    tmp_path: Path,
    language: str,
    source_path: str,
) -> None:
    project = copy_project(tmp_path, language)

    metrics = run_metrics(project, path=str(git_only_bin(tmp_path)))

    assert metrics["module_size_distribution"]["unavailable"]["reason"] == (
        "scc executable is unavailable"
    )
    assert metrics["oversized_module_count"]["unavailable"]["reason"] == (
        "scc executable is unavailable"
    )
    complexity = metrics["complexity_summary"]["value"]
    assert source_path in complexity["files"]
    assert complexity["functions"] == 1
    assert metrics["duplication_summary"]["unavailable"]["reason"] == ("jscpd executable not found")
    for metric_id in (
        "module_size_distribution",
        "oversized_module_count",
        "duplication_summary",
    ):
        assert "value" not in metrics[metric_id]
        assert "Errno" not in metrics[metric_id]["unavailable"]["reason"]
