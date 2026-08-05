"""Happy-path contracts for the optional code-health analyzer dependency."""

from __future__ import annotations

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
