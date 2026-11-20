"""The suite's own raw git writes leave no detached maintenance behind (bug 7fcc-dac7-6f05-4ad1).

At git's defaults ``git commit`` ends by starting ``git maintenance run --auto --detach``, a
child that outlives the commit. Its ``worktree-prune`` task deletes any
``.git/worktrees/<id>/`` that has neither ``locked`` nor ``gitdir`` yet. ``git worktree add``
passes through exactly that state between creating the directory and writing ``locked``, so a
test that committed and then added a worktree raced its own commit's child and failed with
``fatal: could not open '.git/worktrees/-tickets-tracker/locked' for writing``.

``tests/conftest.py`` sets ``GIT_TEST_MAINT_AUTO_DETACH=false`` for the session, as git's own
``t/test-lib.sh`` does, so triggered maintenance finishes before the command that triggered it
returns. Every assertion here reads recorded argv; no timing decides any of them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _git_upkeep import DETACH_SWITCH, assert_git_detaches_by_default, maintenance_children

pytestmark = pytest.mark.unit


def _traced_raw_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Make one raw ``git commit`` the way test fixtures do; return its maintenance children."""
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    trace = tmp_path / "trace2.json"
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "fixture"],
        cwd=repo,
        check=True,
        capture_output=True,
    )  # fmt: skip
    children = maintenance_children(trace)
    # Positive control: the commit did start auto-maintenance, so the checks can fail.
    assert children, f"the commit started no maintenance run; trace at {trace}"
    if not any({"--detach", "--no-detach"} & set(argv) for argv in children):
        pytest.skip(f"this git does not report detaching on the maintenance argv: {children}")
    return children


def test_a_raw_commit_runs_its_maintenance_in_the_foreground(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    children = _traced_raw_commit(tmp_path, monkeypatch)

    detached = [argv for argv in children if "--detach" in argv]
    assert not detached, (
        f"a test's raw git commit left detached maintenance running: {detached}. "
        f"tests/conftest.py must set {DETACH_SWITCH}=false for the session."
    )


def test_the_detach_control_passes_once_a_guard_restores_gits_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(DETACH_SWITCH, raising=False)

    assert_git_detaches_by_default(tmp_path)


def test_the_detach_control_fails_a_guard_that_kept_the_session_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the session setting in force nothing can detach, so a `--detach` guard that did
    not restore git's default would pass without testing anything. The control must fail."""
    monkeypatch.setenv(DETACH_SWITCH, "false")
    _traced_raw_commit(tmp_path / "probe", monkeypatch)  # skips on a git with no such argv

    with pytest.raises(AssertionError, match=DETACH_SWITCH):
        assert_git_detaches_by_default(tmp_path)
