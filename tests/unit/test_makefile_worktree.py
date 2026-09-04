"""`make worktree` must fail loudly when provisioning fails (bug 1738-3816-caca-44a2).

The target created the branch and worktree, delegated provisioning to `make venv` +
`make install`, and then printed "✓ worktree ready" and exited 0 **whatever** that
delegation returned. Its recipe was one shell line whose steps were joined with `;`, and
make applies no `set -e` semantics inside a recipe line — it only inspects the status of
the line as a whole, which is the status of its last command, an `echo`.

The consequence is the "absence of execution reported as success" failure mode: an agent
proceeds into a worktree with no usable `.venv`, and the first symptom is the pre-commit
gate dying on a missing `ruff`/`mypy` — which reads as a code problem, not an environment
one.

These tests drive the **real** `Makefile` in a throwaway sandbox. Two stubs keep the host
out of it: a stub `git` on `PATH`, and the delegated provisioning stubbed by passing
`MAKE=<stub>` as a command-line variable, which overrides the `$(MAKE)` the recipe
expands. No worktree, branch, network fetch, or venv is created outside `tmp_path`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"

SUCCESS_MESSAGE = "✓ worktree ready"
BRANCH = "sandbox-branch"

_GIT_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "{log}"
if [ "$1" = "worktree" ] && [ "$2" = "add" ]; then
    mkdir -p "$3"
    exit {worktree_add_exit}
fi
exit 0
"""

# Stands in for the recursive `$(MAKE) venv` / `$(MAKE) install` the recipe delegates to.
# A successful `venv` materialises `.venv/bin/activate` because the recipe sources it
# between the two delegations.
_MAKE_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "{log}"
if [ "$1" = "venv" ]; then
    if [ {venv_exit} -ne 0 ]; then exit {venv_exit}; fi
    mkdir -p .venv/bin
    printf ':\\n' > .venv/bin/activate
    exit 0
fi
exit {install_exit}
"""


def _write_stub(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _run_worktree(
    tmp_path: Path,
    *,
    worktree_add_exit: int = 0,
    venv_exit: int = 0,
    install_exit: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Run the real `worktree` recipe against stubbed `git` and stubbed provisioning."""

    shutil.copy2(MAKEFILE, tmp_path / "Makefile")
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()

    _write_stub(
        stub_bin / "git",
        _GIT_STUB.format(log=tmp_path / "git-args.txt", worktree_add_exit=worktree_add_exit),
    )
    stub_make = _write_stub(
        stub_bin / "stub-make",
        _MAKE_STUB.format(
            log=tmp_path / "make-args.txt", venv_exit=venv_exit, install_exit=install_exit
        ),
    )

    env = subprocess_env()
    env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        [
            "make",
            "worktree",
            f"name={BRANCH}",
            f"dir={tmp_path / 'worktree'}",
            f"MAKE={stub_make}",
        ],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_provisioning_failure_is_reported_as_failure(tmp_path: Path) -> None:
    # The bug itself: `make venv` failed, `make install` never ran, and the target still
    # exited 0 announcing a ready worktree.
    result = _run_worktree(tmp_path, venv_exit=1)

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert SUCCESS_MESSAGE not in combined
    assert "ERROR" in combined and "provisioning" in combined.lower()


def test_provisioning_failure_names_the_unprovisioned_worktree(tmp_path: Path) -> None:
    # Held out from the exit-status assertion above: a non-zero exit alone still leaves the
    # caller guessing. The failure has to say which directory is half-built, so the next
    # symptom is not a missing ruff/mypy at commit time.
    result = _run_worktree(tmp_path, venv_exit=1)

    combined = result.stdout + result.stderr
    error_lines = [line for line in combined.splitlines() if "ERROR" in line]
    assert any(str(tmp_path / "worktree") in line for line in error_lines), combined
    assert "git worktree remove" in combined, combined


def test_install_failure_is_reported_as_failure(tmp_path: Path) -> None:
    # The other half of provisioning: the editable install / hook wiring.
    result = _run_worktree(tmp_path, install_exit=1)

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert SUCCESS_MESSAGE not in combined


def test_worktree_add_failure_is_reported_as_failure(tmp_path: Path) -> None:
    # A failure before provisioning must abort too, rather than provisioning a directory
    # git never populated.
    result = _run_worktree(tmp_path, worktree_add_exit=1)

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert SUCCESS_MESSAGE not in combined
    assert not (tmp_path / "make-args.txt").exists()


def test_successful_provisioning_still_succeeds(tmp_path: Path) -> None:
    # The fix must not be "always fail": the happy path keeps its exit 0 and its message.
    result = _run_worktree(tmp_path)

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert SUCCESS_MESSAGE in combined
    assert (tmp_path / "make-args.txt").read_text(encoding="utf-8").split() == [
        "venv",
        "install",
    ]
