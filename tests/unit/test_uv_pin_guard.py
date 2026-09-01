"""The uv-pin gate must FAIL on skew, not merely pass on the happy tree [rebar:56b7-b21a-c8ab-4afc].

A guard that can only ever pass has validated nothing. Each test here builds a minimal tree,
introduces exactly ONE way the single-sourced, local-action pin can be defeated, and asserts the
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
      - uses: ./.github/actions/setup-uv
        with:
          enable-cache: true
"""

CHECKSUM_LINES = "\n".join(
    f"        {name}: {value}"
    for name, value in {
        "UV_SHA256_AARCH64_APPLE_DARWIN": (
            "127ebdda7ad953cdf198e964b570ea5771b85467ea93eb7cb6d6f8e6f55408f3"
        ),
        "UV_SHA256_AARCH64_PC_WINDOWS_MSVC": (
            "1611d0f4be72b0a354ad9a6ae954093dd4c91e93e36b8b490326a05a039ffe14"
        ),
        "UV_SHA256_AARCH64_UNKNOWN_LINUX_GNU": (
            "66393193038dd7eb108abd7a218d9cec04ac70ab98242b0720fa94de19223b7c"
        ),
        "UV_SHA256_X86_64_APPLE_DARWIN": (
            "06b8ae1da8c2661c5434507a66f8c2b0b835933bf955b5958a9ac357a37d1959"
        ),
        "UV_SHA256_X86_64_PC_WINDOWS_MSVC": (
            "bf1518af459a3915511a11fdc6e2f43ef9a2afa138b9d498eeb9642fe9d85218"
        ),
        "UV_SHA256_X86_64_UNKNOWN_LINUX_GNU": (
            "788f18abea7c5f55d6216e4f5613fd89d4d59b631efeec117b2b07fe72f1da21"
        ),
    }.items()
)

ACTION_CLEAN = f"""\
name: Setup uv
description: Install the repository-pinned uv release without fetching the setup-uv manifest.
inputs:
  enable-cache:
    description: Restore and save uv's cache directory.
    default: "false"
runs:
  using: composite
  steps:
    - name: Install uv
      shell: pwsh
      env:
{CHECKSUM_LINES}
      run: |
        echo "placeholder"
    - name: Cache uv downloads and wheels
      if: inputs.enable-cache == 'true'
      uses: actions/cache@v6
      with:
        path: ~/.cache/uv
        key: fixture
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
    action = tmp_path / ".github" / "actions" / "setup-uv"
    action.mkdir(parents=True)
    (action / "action.yml").write_text(ACTION_CLEAN, encoding="utf-8")
    return tmp_path


def test_real_repository_passes() -> None:
    """The gate is green on the tree it actually governs."""
    result = _run(REPO_ROOT)
    assert result.returncode == 0, result.stderr


def test_fixture_baseline_passes(tree: Path) -> None:
    """The baseline must pass, or a skew test could pass for the wrong reason."""
    result = _run(tree)
    assert result.returncode == 0, result.stderr


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


def test_astral_setup_uv_call_site_is_rejected(tree: Path) -> None:
    """The local action replaces setup-uv so the manifest-fetching action cannot return."""
    workflow = WORKFLOW_CLEAN.replace("./.github/actions/setup-uv", "astral-sh/setup-uv@v10")
    (tree / ".github" / "workflows" / "build.yml").write_text(workflow, encoding="utf-8")
    result = _run(tree)
    assert result.returncode == 1
    assert "astral-sh/setup-uv" in result.stderr


def test_local_setup_uv_version_override_is_rejected(tree: Path) -> None:
    """A local call site must not defeat pyproject's single-sourced version."""
    workflow = WORKFLOW_CLEAN.replace(
        "          enable-cache: true\n",
        "          enable-cache: true\n          version: 0.12.6\n",
    )
    (tree / ".github" / "workflows" / "build.yml").write_text(workflow, encoding="utf-8")
    result = _run(tree)
    assert result.returncode == 1
    assert "version" in result.stderr


def test_local_setup_uv_version_file_override_is_rejected(tree: Path) -> None:
    """`version-file:` is the sibling override and must be rejected the same way."""
    workflow = WORKFLOW_CLEAN.replace(
        "          enable-cache: true\n",
        "          enable-cache: true\n          version-file: uv.toml\n",
    )
    (tree / ".github" / "workflows" / "build.yml").write_text(workflow, encoding="utf-8")
    result = _run(tree)
    assert result.returncode == 1
    assert "version-file" in result.stderr


def test_missing_local_action_is_rejected(tree: Path) -> None:
    """The guard must fail if workflows point at an action that is not committed."""
    (tree / ".github" / "actions" / "setup-uv" / "action.yml").unlink()
    result = _run(tree)
    assert result.returncode == 1
    assert ".github/actions/setup-uv/action.yml" in result.stderr


def test_manifest_source_in_local_action_is_rejected(tree: Path) -> None:
    """The replacement action must not regain setup-uv's raw manifest fetch."""
    action = tree / ".github" / "actions" / "setup-uv" / "action.yml"
    action.write_text(action.read_text(encoding="utf-8") + "\n# raw.githubusercontent.com\n")
    result = _run(tree)
    assert result.returncode == 1
    assert "raw.githubusercontent.com" in result.stderr


def test_local_action_missing_checksum_is_rejected(tree: Path) -> None:
    """Every supported runner target needs a committed digest before CI can trust it."""
    action = tree / ".github" / "actions" / "setup-uv" / "action.yml"
    checksum_line = (
        "        UV_SHA256_X86_64_PC_WINDOWS_MSVC: "
        "bf1518af459a3915511a11fdc6e2f43ef9a2afa138b9d498eeb9642fe9d85218\n"
    )
    action.write_text(
        action.read_text(encoding="utf-8").replace(checksum_line, ""),
        encoding="utf-8",
    )
    result = _run(tree)
    assert result.returncode == 1
    assert "UV_SHA256_X86_64_PC_WINDOWS_MSVC" in result.stderr


def test_checksum_mismatch_in_local_action_is_rejected(tree: Path) -> None:
    """The committed checksums are part of the pinned artifact contract."""
    action = tree / ".github" / "actions" / "setup-uv" / "action.yml"
    action.write_text(
        action.read_text(encoding="utf-8").replace(
            "788f18abea7c5f55d6216e4f5613fd89d4d59b631efeec117b2b07fe72f1da21",
            "0" * 64,
        ),
        encoding="utf-8",
    )
    result = _run(tree)
    assert result.returncode == 1
    assert "UV_SHA256_X86_64_UNKNOWN_LINUX_GNU" in result.stderr


def test_local_action_emits_acceptance_anchor() -> None:
    """CI greps this exact install liveness line before checking manifest absence."""
    body = (REPO_ROOT / ".github" / "actions" / "setup-uv" / "action.yml").read_text(
        encoding="utf-8"
    )
    assert "Successfully installed uv $version" in body


def test_make_lint_invokes_the_gate() -> None:
    """A gate nothing runs is not a gate.

    `make lint` is the portable, no-CI-provider trigger the project uses for every checker of
    this kind, and CI inherits it through the same step -- so wiring is part of the contract,
    not an implementation detail.
    """
    body = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "python scripts/check_uv_pin.py" in body
