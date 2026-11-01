"""Cross-language code-health values through the public metrics CLI.

Held-out oracles merged from test_metrics_polyglot_heldout: Held-out fallback contracts for
polyglot code-health projects. WITHHELD.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from metrics_polyglot_support import (
    copy_project,
    fake_analyzer_bin,
    git_only_bin,
    run_metrics,
    with_path_prefix,
)

pytestmark = pytest.mark.interface


@pytest.mark.parametrize(
    ("language", "source_path"),
    [
        ("python", "src/main.py"),
        ("typescript", "src/main.ts"),
    ],
)
def test_metrics_cli_reports_polyglot_code_health_values(
    tmp_path: Path,
    language: str,
    source_path: str,
) -> None:
    project = copy_project(tmp_path, language)
    analyzer_bin = fake_analyzer_bin(tmp_path)
    expected_loc = len((project / source_path).read_text(encoding="utf-8").splitlines())

    metrics = run_metrics(project, path=with_path_prefix(analyzer_bin))

    assert metrics["module_size_distribution"]["value"] == {
        "count": 1,
        "near_cap_count": 0,
        "over_cap_count": 0,
        "max_loc": expected_loc,
    }
    assert metrics["oversized_module_count"]["value"] == 0
    complexity = metrics["complexity_summary"]["value"]
    assert source_path in complexity["files"]
    assert complexity["functions"] == 1
    assert complexity["max_ccn"] == 2
    assert metrics["duplication_summary"]["value"] == {
        "clones": 1,
        "percentage": 12.5,
    }
    expected_insertions = sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in (project / "pyproject.toml", project / source_path)
    )
    assert metrics["churn"]["value"] == {
        "insertions": expected_insertions,
        "deletions": 0,
    }
    assert metrics["refactor_to_addition_ratio"]["value"] == 0.0
    attempts = metrics["attempts_per_ticket"]["value"]
    assert list(attempts.values()) == [1]
    assert metrics["first_pass_rate"]["value"] == 1.0


# ── held-out oracles (merged from test_metrics_polyglot_heldout) ──


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
    expected_insertions = sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in (project / "pyproject.toml", project / source_path)
    )
    assert metrics["churn"]["value"] == {
        "insertions": expected_insertions,
        "deletions": 0,
    }
    assert metrics["refactor_to_addition_ratio"]["value"] == 0.0
    attempts = metrics["attempts_per_ticket"]["value"]
    assert list(attempts.values()) == [1]
    assert metrics["first_pass_rate"]["value"] == 1.0
