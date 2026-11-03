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
from itertools import pairwise
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
if [ "$1" = "remote" ] && [ "$2" = "get-url" ]; then
    case " {missing_remotes} " in
        *" $3 "*) printf "error: No such remote '%s'\\n" "$3" >&2; exit 2 ;;
    esac
    printf 'https://example.invalid/%s\\n' "$3"
    exit 0
fi
if [ "$1" = "fetch" ]; then
    case " {missing_remotes} " in
        *" $2 "*)
            printf "fatal: '%s' does not appear to be a git repository\\n" "$2" >&2
            exit 128 ;;
    esac
    exit {fetch_exit}
fi
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
    include_dir: bool = True,
    fetch_exit: int = 0,
    worktree_add_exit: int = 0,
    venv_exit: int = 0,
    install_exit: int = 0,
    missing_remotes: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run the real `worktree` recipe against stubbed `git` and stubbed provisioning."""

    shutil.copy2(MAKEFILE, tmp_path / "Makefile")
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()

    _write_stub(
        stub_bin / "git",
        _GIT_STUB.format(
            log=tmp_path / "git-args.txt",
            fetch_exit=fetch_exit,
            worktree_add_exit=worktree_add_exit,
            missing_remotes=" ".join(missing_remotes),
        ),
    )
    stub_make = _write_stub(
        stub_bin / "stub-make",
        _MAKE_STUB.format(
            log=tmp_path / "make-args.txt", venv_exit=venv_exit, install_exit=install_exit
        ),
    )

    env = subprocess_env()
    env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"
    command = ["make", "worktree", f"name={BRANCH}", f"MAKE={stub_make}"]
    if include_dir:
        command.insert(3, f"dir={tmp_path / 'worktree'}")

    return subprocess.run(
        command,
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


def test_fetch_failure_is_reported_as_failure(tmp_path: Path) -> None:
    # The earliest fallible step. It has its own named handler, so it gets its own
    # contract: a stale-or-absent origin/main must not silently become a worktree
    # branched from whatever the checkout already had.
    result = _run_worktree(tmp_path, fetch_exit=1)

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert SUCCESS_MESSAGE not in combined
    assert "git fetch origin" in combined
    assert "worktree add" not in (tmp_path / "git-args.txt").read_text(encoding="utf-8")


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


def test_default_worktree_path_stays_inside_repo_sandbox(tmp_path: Path) -> None:
    result = _run_worktree(tmp_path, include_dir=False)

    combined = result.stdout + result.stderr
    outside_default = tmp_path.parent / BRANCH
    try:
        assert result.returncode == 0, combined
        assert f"worktree add .rebar/worktrees/{BRANCH} -b {BRANCH} origin/main" in (
            tmp_path / "git-args.txt"
        ).read_text(encoding="utf-8")
        assert "/bin/sh: branch:" not in combined
        assert not outside_default.exists()
    finally:
        shutil.rmtree(outside_default, ignore_errors=True)


def test_worktree_recipe_does_not_feed_make_comments_to_shell() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8").splitlines()
    recipe_start = next(
        index for index, line in enumerate(makefile) if line.startswith("worktree:")
    )
    next_target = next(
        index
        for index, line in enumerate(makefile[recipe_start + 1 :], start=recipe_start + 1)
        if line and not line.startswith(("\t", " "))
    )
    recipe_lines = makefile[recipe_start + 1 : next_target]

    hazardous_pairs = [
        (line, following)
        for line, following in pairwise(recipe_lines)
        if line.rstrip().endswith("\\") and following.strip().startswith("@#")
    ]

    assert hazardous_pairs == []
    assert '\ttarget_dir="$(if $(dir),$(dir),.rebar/worktrees/$(name))"; \\' in recipe_lines


# --- Missing remotes (bug 1712-f158-9314-420a) ------------------------------------------
#
# A copied linked worktree shares the checkout's `.git/config`, and `git remote remove` run
# in such a copy deleted both remotes from the shared config. `make worktree` then surfaced
# only git's raw "'origin' does not appear to be a git repository", which reads like a
# network or permissions fault. The recipe must name the missing remote itself.

ORIGIN_URL = "https://github.com/navapbc/rebar.git"
RUNBOOK = "docs/local-dev-env.md"
RAW_GIT_MESSAGE = "does not appear to be a git repository"


def test_missing_origin_is_named_before_git_fetch_runs(tmp_path: Path) -> None:
    result = _run_worktree(tmp_path, missing_remotes=("origin", "gerrit"))

    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert SUCCESS_MESSAGE not in combined
    error_lines = [line for line in combined.splitlines() if "ERROR" in line]
    assert any("origin" in line and "remote" in line for line in error_lines), combined
    assert RAW_GIT_MESSAGE not in combined, combined
    git_calls = (tmp_path / "git-args.txt").read_text(encoding="utf-8").splitlines()
    assert not any(call.startswith(("fetch", "worktree add")) for call in git_calls), git_calls


def test_missing_origin_prints_the_restore_command_and_runbook(tmp_path: Path) -> None:
    result = _run_worktree(tmp_path, missing_remotes=("origin",))

    combined = result.stdout + result.stderr
    assert f"git remote add origin {ORIGIN_URL}" in combined, combined
    assert RUNBOOK in combined, combined


def test_missing_gerrit_warns_but_still_creates_the_worktree(tmp_path: Path) -> None:
    # Creating a worktree never talks to gerrit; only the later push does. So a missing
    # gerrit remote is announced, not fatal.
    result = _run_worktree(tmp_path, missing_remotes=("gerrit",))

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert SUCCESS_MESSAGE in combined
    warning_lines = [line for line in combined.splitlines() if "WARNING" in line]
    assert any("gerrit" in line for line in warning_lines), combined
    assert RUNBOOK in combined, combined


def test_both_remotes_present_prints_no_remote_warning(tmp_path: Path) -> None:
    result = _run_worktree(tmp_path)

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "WARNING" not in combined, combined


def test_runbook_records_the_remote_urls_the_recipe_relies_on() -> None:
    recipe = MAKEFILE.read_text(encoding="utf-8")
    runbook = (REPO_ROOT / RUNBOOK).read_text(encoding="utf-8")

    assert ORIGIN_URL in recipe
    assert ORIGIN_URL in runbook
    assert "https://rebar.solutions.navateam.com/a/rebar" in runbook
    assert "ssh://<you>@rebar.solutions.navateam.com:29418/rebar" in runbook
