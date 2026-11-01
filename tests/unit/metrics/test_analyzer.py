"""Held-out contracts for the code-health analyzer result shape (ticket 3b30)."""

from __future__ import annotations

import subprocess
import sys

import pytest

import rebar.metrics.analyzer as analyzer_module

pytestmark = pytest.mark.unit


def test_analyzer_import_is_additive_and_unsupported_signals_default_to_none() -> None:
    result = analyzer_module.AnalyzerResult(loc={"files": {}, "max_loc": 0})
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from rebar.metrics.registry import REGISTRY\n"
                "before = tuple(spec.id for spec in REGISTRY)\n"
                "import rebar.metrics.analyzer\n"
                "after = tuple(spec.id for spec in REGISTRY)\n"
                "raise SystemExit(0 if after == before else 1)\n"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert result.loc == {"files": {}, "max_loc": 0}
    assert result.complexity is None
    assert result.duplication is None
