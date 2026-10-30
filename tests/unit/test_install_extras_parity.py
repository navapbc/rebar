"""`make install` installs the extras CI's gating pytest cell installs (bug 9b28-415d-0780-4b2a).

Tests guarded by ``pytest.importorskip("fastapi")`` and friends skip when their extra is
missing. CI's gating cell installs those extras and sets ``REBAR_REQUIRE_EXTRAS=1``, so a
local ``make verify`` on a lean venv skipped tests CI then failed (Gerrit 3141). The Makefile
names the test-bearing extras once, as ``TEST_EXTRAS``; these tests hold that list equal to
the gating cell's and check both install targets expand it.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MAKEFILE = _REPO_ROOT / "Makefile"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "_build-and-test.yml"


def _gating_cell_extras(workflow: str) -> set[str]:
    # The gating job is the one that promises every extra is installed; several jobs share
    # the install step's name, so match on that promise rather than on a step name.
    jobs = yaml.safe_load(workflow)["jobs"].values()
    steps = next(
        job["steps"]
        for job in jobs
        if any("REBAR_REQUIRE_EXTRAS" in (step.get("env") or {}) for step in job.get("steps", []))
    )
    sync = next(step["run"] for step in steps if "uv sync" in step.get("run", ""))
    return set(re.findall(r"--extra\s+([\w-]+)", sync))


def _test_extras(makefile: str) -> set[str]:
    match = re.search(r"^TEST_EXTRAS\s*:?=\s*(.*)$", makefile, re.MULTILINE)
    assert match, "Makefile defines no TEST_EXTRAS variable"
    return set(match.group(1).split())


def _recipe(makefile: str, target: str) -> str:
    match = re.search(rf"^{re.escape(target)}:.*\n((?:\t.*\n|\n)*)", makefile, re.MULTILINE)
    assert match, f"Makefile has no {target} target"
    return match.group(1)


def test_test_extras_match_the_gating_cell() -> None:
    gating = _gating_cell_extras(_WORKFLOW.read_text(encoding="utf-8"))
    # `install` names `dev` separately, and `metrics` is an install-only extra.
    assert _test_extras(_MAKEFILE.read_text(encoding="utf-8")) == gating - {"dev"}


def test_install_syncs_the_test_extras() -> None:
    recipe = _recipe(_MAKEFILE.read_text(encoding="utf-8"), "install")
    sync = next(line for line in recipe.splitlines() if "uv sync" in line)
    assert "TEST_EXTRAS" in sync, f"install's uv sync does not expand TEST_EXTRAS: {sync.strip()}"


def test_install_unlocked_installs_the_test_extras() -> None:
    recipe = _recipe(_MAKEFILE.read_text(encoding="utf-8"), "install-unlocked")
    pip = next(line for line in recipe.splitlines() if "pip install" in line)
    assert "TEST_EXTRAS" in pip, f"install-unlocked does not expand TEST_EXTRAS: {pip.strip()}"


def test_parsers_see_a_removed_extra() -> None:
    # The comparison must notice drift on either side, not just pass on today's files.
    workflow = _WORKFLOW.read_text(encoding="utf-8").replace(" --extra ui", "", 1)
    assert "ui" not in _gating_cell_extras(workflow)
    assert _test_extras("TEST_EXTRAS := reviewbot ui\n") == {"reviewbot", "ui"}
