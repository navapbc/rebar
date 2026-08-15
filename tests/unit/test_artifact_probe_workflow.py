"""Held-out exact-provenance contracts for the reusable artifact probe."""

from __future__ import annotations

from pathlib import Path

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "_artifact-probe.yml"


def test_artifact_probe_builds_the_final_checkout_commit() -> None:
    workflow = _WORKFLOW.read_text(encoding="utf-8")

    assert "REBAR_EXPECTED_COMMIT=$(git rev-parse --short HEAD)" in workflow
    assert 'REBAR_BUILD_COMMIT="$REBAR_EXPECTED_COMMIT" python -m build' in workflow
    assert 'REBAR_BUILD_COMMIT="${GITHUB_SHA::7}"' not in workflow


def test_artifact_probe_requires_exact_commit_from_both_distributions() -> None:
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    exact_probe = "assert _build_info.COMMIT == os.environ['REBAR_EXPECTED_COMMIT']"

    assert workflow.count(exact_probe) == 2
    assert "assert _build_info.COMMIT," not in workflow
