"""Held-out missing-extra and import-cleanliness contracts for ticket 9597."""

from __future__ import annotations

import json
import subprocess
import sys

from rebar.metrics import analyzer as analyzer_module
from rebar.metrics.registry import Unavailable


def test_load_lizard_converts_missing_extra_to_unavailable(block_extra) -> None:
    assert hasattr(analyzer_module, "load_lizard"), "the optional lizard loader is not implemented"
    block_extra("lizard")

    result = analyzer_module.load_lizard(accruing_since="2026-07-23")

    assert isinstance(result, Unavailable)
    assert result.accruing_since == "2026-07-23"
    assert "metrics" in result.reason
    assert "nava-rebar[metrics]" in result.reason


def test_core_import_is_clean_and_missing_lizard_degrades_in_subprocess() -> None:
    code = r"""
import importlib.abc
import json
import sys

class BlockLizard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "lizard" or fullname.startswith("lizard."):
            raise ModuleNotFoundError(fullname)
        return None

sys.meta_path.insert(0, BlockLizard())
import rebar
import rebar.metrics
from rebar.metrics import analyzer

if not hasattr(analyzer, "load_lizard"):
    print("MISSING_LOAD_LIZARD")
    raise SystemExit(7)
before = "lizard" in sys.modules
result = analyzer.load_lizard(accruing_since="2026-07-23")
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
        "accruing_since": "2026-07-23",
    }
    assert "metrics" in result["reason"]
    assert "nava-rebar[metrics]" in result["reason"]
