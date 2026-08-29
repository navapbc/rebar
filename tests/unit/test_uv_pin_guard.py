"""The uv-pin gate must FAIL on skew, not merely pass on the happy tree [rebar:56b7-b21a-c8ab-4afc].

A guard that can only ever pass has validated nothing. Each test here builds a minimal tree,
introduces exactly ONE of the four ways the single-sourced pin can be defeated, and asserts the
checker rejects it and names the offending location — so the gate's failing state is proven
rather than assumed. The happy-path test additionally runs against this repository's real root,
so the gate and the tree it governs cannot drift apart silently.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "check_uv_pin.py"

PYPROJECT_PINNED = """\
[project]
name = "fixture"
version = "0.1.0"

[tool.uv]
required-version = "==0.12.7"
"""

WORKFLOW_CLEAN = """\
name: fixture
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
      - uses: astral-sh/setup-uv@ae62891fec2bb8e7d6c99fc78c9fec3a63790f8d
        with:
          enable-cache: true
"""


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the checker as a subprocess against ``root``, exactly as ``make lint`` does."""
    return subprocess.run(
        [sys.executable, str(GATE), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A minimal, correctly pinned repository: the baseline every skew mutates."""
    (tmp_path / "pyproject.toml").write_text(PYPROJECT_PINNED, encoding="utf-8")
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "build.yml").write_text(WORKFLOW_CLEAN, encoding="utf-8")
    return tmp_path


def test_real_repository_passes() -> None:
    """The gate is green on the tree it actually governs."""
    result = _run(REPO_ROOT)
    assert result.returncode == 0, result.stderr


def test_fixture_baseline_passes(tree: Path) -> None:
    """The baseline must pass, or a skew test could pass for the wrong reason."""
    result = _run(tree)
    assert result.returncode == 0, result.stderr


def test_per_call_site_version_input_is_rejected(tree: Path) -> None:
    """A `version:` input on ONE call site diverges that job from the single source.

    This is the skew the ticket calls out by name: the pin is single-sourced, so the check that
    replaces a per-site consistency comparison must fail when a site stops resolving to it.
    """
    workflow = tree / ".github" / "workflows" / "build.yml"
    workflow.write_text(
        WORKFLOW_CLEAN.replace(
            "          enable-cache: true\n",
            '          enable-cache: true\n          version: "0.11.0"\n',
        ),
        encoding="utf-8",
    )
    result = _run(tree)
    assert result.returncode == 1
    assert "build.yml" in result.stderr
    assert "version" in result.stderr


def test_version_file_input_is_rejected(tree: Path) -> None:
    """`version-file:` is the sibling override and must be rejected the same way."""
    workflow = tree / ".github" / "workflows" / "build.yml"
    workflow.write_text(
        WORKFLOW_CLEAN.replace(
            "          enable-cache: true\n",
            "          enable-cache: true\n          version-file: uv.toml\n",
        ),
        encoding="utf-8",
    )
    result = _run(tree)
    assert result.returncode == 1
    assert "version-file" in result.stderr


@pytest.mark.parametrize("specifier", ['">=0.12.7"', '"~=0.12.0"', '"0.12.7"', '"==0.12.*"'])
def test_range_specifier_is_rejected(tree: Path, specifier: str) -> None:
    """A range still triggers the manifest fetch, so it must not read as pinned.

    `"0.12.7"` (no operator) is included deliberately: uv accepts it, so it looks correct, but
    the action's `normalizeVersionSpecifier` only strips a leading `==` — this gate insists on
    the form that is unambiguously exact to BOTH readers.
    """
    (tree / "pyproject.toml").write_text(
        PYPROJECT_PINNED.replace('"==0.12.7"', specifier), encoding="utf-8"
    )
    result = _run(tree)
    assert result.returncode == 1
    assert "exact" in result.stderr


def test_missing_pin_is_rejected(tree: Path) -> None:
    """Deleting the section restores the original outage, so it must fail loudly."""
    (tree / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    result = _run(tree)
    assert result.returncode == 1
    assert "required-version" in result.stderr


def test_root_uv_toml_is_rejected(tree: Path) -> None:
    """A root uv.toml shadows the pin for setup-uv AND for uv itself."""
    (tree / "uv.toml").write_text('required-version = "==0.11.0"\n', encoding="utf-8")
    result = _run(tree)
    assert result.returncode == 1
    assert "uv.toml" in result.stderr


def test_make_lint_invokes_the_gate() -> None:
    """A gate nothing runs is not a gate.

    `make lint` is the portable, no-CI-provider trigger the project uses for every checker of
    this kind, and CI inherits it through the same step -- so wiring is part of the contract,
    not an implementation detail.
    """
    body = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "python scripts/check_uv_pin.py" in body
