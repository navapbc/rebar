"""The dev-env interpreter pin: `make venv` builds on the Python CI tests (bug a5f5).

`make worktree` used to provision with `python3 -m venv .venv`, inheriting whatever the host's
ambient `python3` happened to be. On the machine where this was found that was 3.14.6 while CI
tested 3.11/3.12/3.13, so **every** worktree the repo's own one-command setup produced ran an
interpreter CI never exercised. The failure mode is corrosive rather than loud: local red that
CI cannot reproduce trains people to discount local failures in general.

These contracts pin two things that must not drift apart — what `make venv` asks for, and what
CI actually runs — with the value single-sourced in `.github/python-version.txt`, the same
discipline as `.github/git-version-floor.txt` and `.github/module-size-limit.txt`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PIN_FILE = REPO_ROOT / ".github" / "python-version.txt"
MAKEFILE = REPO_ROOT / "Makefile"
BUILD_AND_TEST = REPO_ROOT / ".github" / "workflows" / "_build-and-test.yml"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

_PIN_RE = re.compile(r"^\d+\.\d+$")
_MATRIX_RE = re.compile(r"^\s*python-version:\s*\[(?P<versions>[^\]]*)\]", re.MULTILINE)
_SCALAR_PIN_RE = re.compile(r"^\s*python-version:\s*\"(?P<version>\d+\.\d+)\"", re.MULTILINE)


def _pinned_version() -> str:
    return PIN_FILE.read_text(encoding="utf-8").strip()


def _ci_matrix_versions() -> list[str]:
    text = BUILD_AND_TEST.read_text(encoding="utf-8")
    match = _MATRIX_RE.search(text)
    assert match is not None, "no python-version matrix found in _build-and-test.yml"
    return [entry.strip().strip('"').strip("'") for entry in match["versions"].split(",")]


def test_pin_file_holds_a_bare_major_minor_version() -> None:
    assert PIN_FILE.is_file(), f"{PIN_FILE} must single-source the dev interpreter version"
    assert _PIN_RE.match(_pinned_version()), (
        f"{PIN_FILE} must contain a bare MAJOR.MINOR version, got {_pinned_version()!r}"
    )


def test_pinned_version_is_one_ci_actually_tests() -> None:
    # The anti-drift guarantee the pin exists for: dropping this version from the CI matrix
    # must fail here rather than silently leave every fresh worktree on an untested rev.
    assert _pinned_version() in _ci_matrix_versions()


def _setup_python_pins(workflow: Path) -> list[str]:
    """Scalar `python-version:` values on `actions/setup-python` steps in *workflow*.

    Deliberately narrow: matrix `exclude:` entries also carry a `python-version:` key, and
    those name versions the matrix is *dropping*, not the interpreter a job installs.
    """

    pins: list[str] = []
    lines = workflow.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if "actions/setup-python@" not in line:
            continue
        for follower in lines[index + 1 : index + 4]:
            match = _SCALAR_PIN_RE.match(follower)
            if match is not None:
                pins.append(match["version"])
    return pins


def test_every_setup_python_pin_matches_the_single_source() -> None:
    pinned = _pinned_version()
    drifted: list[str] = []
    for workflow in sorted(WORKFLOWS.glob("*.y*ml")):
        drifted.extend(
            f"{workflow.name}: {version}"
            for version in _setup_python_pins(workflow)
            if version != pinned
        )
    assert not drifted, f"setup-python pins disagree with {PIN_FILE.name}: {drifted}"


def _makefile_recipe_lines() -> str:
    """The Makefile with `#` comment lines dropped — what make actually executes."""

    return "\n".join(
        line
        for line in MAKEFILE.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().lstrip("@").startswith("#")
    )


def test_makefile_does_not_provision_from_ambient_python3() -> None:
    # The defect itself, stated as a contract: no recipe may build the dev venv from whatever
    # `python3` the host happens to resolve.
    assert "python3 -m venv" not in _makefile_recipe_lines()


def _sandbox(tmp_path: Path, *, uv_exit: int) -> tuple[Path, dict[str, str]]:
    """A throwaway tree with the real Makefile and pin file, and a recording `uv` stub."""

    shutil.copy2(MAKEFILE, tmp_path / "Makefile")
    (tmp_path / ".github").mkdir()
    shutil.copy2(PIN_FILE, tmp_path / ".github" / PIN_FILE.name)

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub_uv = stub_bin / "uv"
    stub_uv.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{tmp_path}/uv-args.txt"\nexit {uv_exit}\n',
        encoding="utf-8",
    )
    stub_uv.chmod(0o755)

    env = subprocess_env()
    env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"
    return stub_bin, env


def _run_make_venv(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "venv"],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_make_venv_asks_for_the_pinned_interpreter(tmp_path: Path) -> None:
    _, env = _sandbox(tmp_path, uv_exit=0)

    result = _run_make_venv(tmp_path, env)

    assert result.returncode == 0, result.stderr
    invocation = (tmp_path / "uv-args.txt").read_text(encoding="utf-8")
    assert f"venv --python {_pinned_version()} .venv" in invocation


def test_make_venv_fails_loudly_rather_than_falling_back(tmp_path: Path) -> None:
    # Held out from the pin assertion above: an unavailable interpreter must be a hard,
    # explanatory failure. A silent fallback to ambient python3 is the original bug.
    _, env = _sandbox(tmp_path, uv_exit=1)

    result = _run_make_venv(tmp_path, env)

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert _pinned_version() in combined
    assert not (tmp_path / ".venv").exists()


def test_worktree_target_provisions_through_the_pinned_venv_target() -> None:
    recipe = _makefile_recipe_lines().split("\nworktree:", 1)[1].split("\n\n", 1)[0]
    assert "$(MAKE) venv" in recipe
    assert "$(MAKE) install" in recipe
    assert "python3 -m venv" not in recipe
