"""Happy-path contracts for the optional code-health analyzer dependency."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import tomllib

from rebar import _optional
from rebar.metrics import analyzer as analyzer_module


def test_metrics_extra_declares_lizard_and_probe() -> None:
    pyproject = Path(__file__).parents[3] / "pyproject.toml"
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]

    assert project["optional-dependencies"]["metrics"] == ["lizard>=1.23"]
    probe, blurb = _optional.EXTRAS["metrics"]
    assert probe == "lizard"
    assert "code-health" in blurb


def test_load_lizard_returns_available_module(monkeypatch) -> None:
    assert hasattr(analyzer_module, "load_lizard"), "the optional lizard loader is not implemented"
    fake_lizard = ModuleType("lizard")
    monkeypatch.setitem(sys.modules, "lizard", fake_lizard)

    result = analyzer_module.load_lizard(accruing_since="2026-07-23")

    assert result is fake_lizard
