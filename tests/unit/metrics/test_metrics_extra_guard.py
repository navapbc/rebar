"""Happy-path contracts for the optional code-health analyzer dependency.

Held-out oracles merged from test_metrics_extra_guard_heldout: Held-out missing-extra and
import-cleanliness contracts for ticket 9597.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import tomllib

from rebar import _optional


def test_metrics_extra_declares_lizard_and_probe() -> None:
    pyproject = Path(__file__).parents[3] / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]

    assert project["optional-dependencies"]["metrics"] == ["lizard>=1.23"]
    probe, blurb = _optional.EXTRAS["metrics"]
    assert probe == "lizard"
    assert "code-health" in blurb


# ── held-out oracles (merged from test_metrics_extra_guard_heldout) ──


def test_core_import_is_clean_and_missing_lizard_degrades_in_subprocess() -> None:
    code = r"""
import importlib.abc
import json
import sys
from pathlib import Path

class BlockLizard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "lizard" or fullname.startswith("lizard."):
            raise ModuleNotFoundError(fullname)
        return None

sys.meta_path.insert(0, BlockLizard())
import rebar
import rebar.metrics
from rebar.metrics.analyzers import lizard_complexity

before = "lizard" in sys.modules
result = lizard_complexity.analyze(Path("."))
print(json.dumps({
    "before": before,
    "kind": type(result).__name__,
    "reason": result.reason,
    "accruing_since": result.accruing_since,
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert {
        "before": result["before"],
        "kind": result["kind"],
        "accruing_since": result["accruing_since"],
    } == {
        "before": False,
        "kind": "Unavailable",
        "accruing_since": "2026-01-01T00:00:00+00:00",
    }
    assert "metrics" in result["reason"]
    assert "nava-rebar[metrics]" in result["reason"]
