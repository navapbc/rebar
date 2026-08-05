"""Held-out reconciliation between the scc-backed size metric and the CI module-size gate.

Ticket c5b3 (rapid-cuboid-velvetcrab). ``module_size_distribution`` reported a CONFIDENT ZERO
on a repository whose largest module is at the cap, because the scc invocation omitted
``--by-file``. Two things must hold once that is fixed:

* the file-set narrowing is PER-PROJECT CONFIGURATION, dogfooded here rather than hardcoded
  into the polyglot adapter (the operator's ruling on AC3; a config knob with no live read
  site is the dce2 failure mode), and
* the number it reports must agree with what the CI ``Module-size gate`` actually enforces
  (AC4) — the gate counts ``wc -l`` over ``src/rebar/**/*.py`` against
  ``.github/module-size-limit.txt``.
"""

from __future__ import annotations

import shutil

import pytest

from rebar import config
from rebar._config_schema import CodeHealthConfig
from rebar.metrics.analyzer import AnalyzerResult
from rebar.metrics.analyzers import scc_loc
from tests.unit.test_module_size_contract import (
    REPO_ROOT,
    SRC_ROOT,
    compute_over_cap_modules,
    read_limit,
)

pytestmark = pytest.mark.unit


def _code_health() -> CodeHealthConfig:
    return config.load_config(root=REPO_ROOT).code_health


def test_this_project_configures_the_narrowing_rather_than_hardcoding_it() -> None:
    """The knob has a live read site AND is dogfooded in this repository's own config."""

    value = _code_health()

    assert value.include_extensions == ["py"], (
        "rebar's own rebar.toml must configure the file types its module-size policy covers; "
        "an unconfigured knob is an inert knob"
    )
    assert value.scan_roots == ["src/rebar"], (
        "the directory axis stays in scan_roots — it is the CI gate's scan root"
    )
    assert value.size_cap == read_limit(), (
        "the metric's cap must be the same number the CI module-size gate enforces"
    )


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("scc") is None, reason="scc is not installed")
def test_metric_file_set_and_loc_match_the_ci_gate() -> None:
    """AC4: same files, same line counts, same over-cap verdict as the gate."""

    value = _code_health()
    result = scc_loc.analyze(
        REPO_ROOT,
        value.scan_roots,
        include_extensions=value.include_extensions,
    )

    assert isinstance(result, AnalyzerResult), f"expected a measurement, got {result!r}"
    measured = result.loc["files"]
    assert measured, "src/rebar demonstrably contains Python modules"

    gate_files = {
        path.relative_to(REPO_ROOT).as_posix(): path.read_text(
            encoding="utf-8", errors="surrogateescape"
        ).count("\n")
        for path in SRC_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
    }

    assert set(measured) == set(gate_files), (
        "the metric must measure exactly the file set the CI gate enforces over"
    )
    assert measured == gate_files, "per-file LOC must equal the gate's wc -l count"

    cap = read_limit()
    assert result.loc["max_loc"] == max(gate_files.values())
    over_cap = {path for path, loc in measured.items() if loc > cap}
    assert over_cap == set(compute_over_cap_modules(SRC_ROOT, cap=cap))


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("scc") is None, reason="scc is not installed")
def test_configured_scan_excludes_bundled_assets_and_resource_wordlists() -> None:
    """AC3: the resource .txt and the bundled editor JS are out of the module-size metric."""

    value = _code_health()
    result = scc_loc.analyze(
        REPO_ROOT,
        value.scan_roots,
        include_extensions=value.include_extensions,
    )

    assert isinstance(result, AnalyzerResult)
    measured = result.loc["files"]
    excluded = [
        "src/rebar/_engine/resources/ticket-wordlist-v2.txt",
        "src/rebar/llm/workflow/editor_assets/dist/editor.js",
        "src/rebar/llm/workflow/editor_assets/src/rebarProvider.jsx",
    ]
    for path in excluded:
        assert (REPO_ROOT / path).exists(), f"fixture assumption broken: {path} is gone"
        assert path not in measured, f"{path} is not a module the size policy governs"
    assert all(path.endswith(".py") for path in measured)
