"""A git watchdog timeout ends ``rebar init`` instead of being read as an answer.

Bug ``75fe-db94-fcee-44da`` (corrupting-plaster-seagull). Init's bootstrap git calls read a
timed-out call's empty output as "not a worktree", "no branch" or "unset" and went on to
the next call, each paying its own bound; reproduced with a stalling git shim, a mounted
store whose ``rev-parse`` timed out was pruned and its tracker directory deleted, and an
init stopped after ``worktree add --orphan`` reported success while the next init never
made the initial commit. The git seam is stubbed to return the watchdog's own result
(code 124), so no test sleeps.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from rebar._cli import _init as cli_init
from rebar._commands import _init_ensures, init
from rebar._store.git_outcome import WATCHDOG_TIMEOUT_RC

pytestmark = pytest.mark.unit


def _timed_out(args: tuple[str, ...]) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        ["git", *args], WATCHDOG_TIMEOUT_RC, "", "git timed out after 120s"
    )


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "t@e.com"),
        ("config", "user.name", "t"),
        ("commit", "-q", "--allow-empty", "-m", "i"),
    ):
        assert _git(path, *args).returncode == 0
    monkeypatch.setenv("REBAR_ROOT", str(path))
    return path


def _stall(monkeypatch: pytest.MonkeyPatch, after: Callable[[tuple], bool] | None = None):
    """Make every init/ensure git call time out (from the call ``after`` matches, if given).
    Return the list of calls that timed out."""
    real = init.run_git_write
    stalled: list[tuple] = []
    state = {"on": after is None}

    def run(cwd, *args, **kwargs):
        if state["on"]:
            stalled.append(args)
            return _timed_out(args)
        result = real(cwd, *args, **kwargs)
        if after is not None and after(args):
            state["on"] = True
        return result

    monkeypatch.setattr(init, "run_git_write", run)
    monkeypatch.setattr(_init_ensures, "run_git_write", run)
    return stalled


def _pending(repo: Path) -> Path:
    return repo / ".git" / "ticket-init.pending"


def test_a_fully_stalled_fresh_init_stops_after_one_bound(repo, monkeypatch, capsys):
    stalled = _stall(monkeypatch)
    assert init.init_core(str(repo), silent=True) == 1
    assert len(stalled) == 1, f"init kept calling a stalled git: {stalled}"
    err = capsys.readouterr().err
    assert "timed out during rebar init" in err and "Re-run 'rebar init'" in err
    assert not (repo / ".tickets-tracker").exists()


def test_a_timed_out_probe_never_deletes_a_mounted_tracker(repo, monkeypatch):
    assert init.init_core(str(repo), silent=True) == 0
    tracker = repo / ".tickets-tracker"
    stalled = _stall(monkeypatch)
    assert init.init_core(str(repo), silent=True) == 1
    assert stalled == [("rev-parse", "--is-inside-work-tree")]
    assert (tracker / ".git").is_file(), "a timed-out rev-parse removed the tracker"


def test_an_init_stopped_mid_bootstrap_is_finished_by_the_next_init(repo, monkeypatch):
    with monkeypatch.context() as m:
        stalled = _stall(m, after=lambda args: args[:3] == ("worktree", "add", "--orphan"))
        assert init.init_core(str(repo), silent=True) == 1
    assert len(stalled) == 1
    assert _pending(repo).exists()
    tracker = repo / ".tickets-tracker"
    assert _git(tracker, "rev-parse", "--verify", "-q", "HEAD").returncode != 0

    assert init.init_core(str(repo), silent=True) == 0

    root = _git(tracker, "log", "--reverse", "--format=%s").stdout.splitlines()
    assert root[0] == "chore: initialize ticket tracker"
    assert _git(tracker, "ls-files", ".pre-commit-config.yaml").stdout.strip()
    assert _git(tracker, "config", "commit.gpgsign").stdout.strip() == "false"
    assert not _pending(repo).exists()

    head = _git(tracker, "rev-parse", "HEAD").stdout
    assert init.init_core(str(repo), silent=True) == 0
    assert _git(tracker, "rev-parse", "HEAD").stdout == head, "a finished store re-bootstrapped"


def test_a_completed_init_leaves_no_pending_marker(repo):
    assert init.init_core(str(repo), silent=True) == 0
    assert not _pending(repo).exists()


def test_auto_init_reports_a_timeout_instead_of_creating_a_store(repo, monkeypatch, capsys):
    calls: list[str] = []
    monkeypatch.setattr(init, "init_core", lambda *a, **k: calls.append("init_core") or 0)
    monkeypatch.setattr(init, "_main_worktree_tracker", lambda repo: None)
    _stall(monkeypatch)
    with pytest.raises(SystemExit) as exited:
        cli_init._create_tracker(str(repo))
    assert exited.value.code == 1
    assert calls == []
    assert "nothing was initialized" in capsys.readouterr().err


def test_a_timed_out_rebase_continue_is_not_followed_by_abort(repo, monkeypatch):
    assert init.init_core(str(repo), silent=True) == 0
    tracker = repo / ".tickets-tracker"
    git_dir = Path(_git(tracker, "rev-parse", "--git-dir").stdout.strip())
    git_dir = git_dir if git_dir.is_absolute() else tracker / git_dir
    (git_dir / "rebase-merge").mkdir()
    try:
        stalled = _stall(monkeypatch, after=lambda args: args[:2] == ("rev-parse", "--git-dir"))
        assert init.init_core(str(repo), silent=True) == 1
        assert [a for a in stalled if "--abort" in a] == []
        assert len(stalled) == 1 and "--continue" in stalled[0]
    finally:
        os.rmdir(git_dir / "rebase-merge")


def test_the_remote_probes_go_through_the_bounded_seam(repo, monkeypatch):
    monkeypatch.setattr(init, "_gc_config_unit", lambda _repo: None)  # skip the pin
    stalled = _stall(monkeypatch)
    assert init.init_core(str(repo), silent=True) == 1
    assert stalled and stalled[0][:2] == ("remote", "get-url")


def test_a_failed_root_commit_keeps_the_bootstrap_pending(repo, monkeypatch, capsys):
    real = init.run_git_write

    def run(cwd, *args, **kwargs):
        if args[:2] == ("commit", "--allow-empty"):
            return subprocess.CompletedProcess(["git", *args], 1, "", "cannot lock ref")
        return real(cwd, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(init, "run_git_write", run)
        assert init.init_core(str(repo), silent=True) == 1
    assert "could not create the ticket store's first commit" in capsys.readouterr().err
    assert _pending(repo).exists()
    assert init.init_core(str(repo), silent=True) == 0
    tracker = repo / ".tickets-tracker"
    root = _git(tracker, "log", "--reverse", "--format=%s").stdout.splitlines()
    assert root[0] == "chore: initialize ticket tracker"
    assert not _pending(repo).exists()


def test_a_resume_waits_for_the_init_lock(repo, monkeypatch):
    with monkeypatch.context() as m:
        _stall(m, after=lambda args: args[:3] == ("worktree", "add", "--orphan"))
        assert init.init_core(str(repo), silent=True) == 1
    lock = repo / ".git" / "ticket-init.lock"
    lock.mkdir()  # another init still holds the lock
    monkeypatch.setattr(init, "_acquire_init_lock", lambda lock_dir: False)
    try:
        assert init.init_core(str(repo), silent=True) == 1
        assert _pending(repo).exists(), "a resume ran without the init lock"
    finally:
        lock.rmdir()
