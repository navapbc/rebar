"""A snapshot git call killed by its watchdog is reported as a timeout, never as an answer.

Bug educatory-toxic-eel: ``git_fetch.git_run`` folded ``TimeoutExpired`` into a private
rc-124 result, so a timed-out ``git rev-parse`` surfaced as "cannot resolve ref" and a
timed-out ``git remote`` as "no such remote". These tests fire the real watchdog: a ``git``
wrapper on ``PATH`` replaces the targeted subcommand with ``exec sleep`` (so the killed child
is the sleeper itself and nothing is orphaned) and ``_GIT_TIMEOUT`` is shrunk to 1 second.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from rebar._snapshot import git_fetch, repo_snapshot
from rebar._snapshot.git_fetch import SnapshotError, SnapshotRefError
from rebar._store import git_outcome

_BOUND_S = 1


@pytest.fixture
def repo(tmp_path: Path) -> str:
    """A one-commit repo whose ``origin`` remote exists (it points at itself)."""
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    identity = ["-c", "user.name=t", "-c", "user.email=t@e"]
    subprocess.run(
        ["git", "-C", str(root), *identity, "commit", "-q", "--allow-empty", "-m", "c"],
        check=True,
    )
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", str(root)], check=True)
    return str(root)


@pytest.fixture
def slow_git(repo: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Return a setter that makes one git subcommand hang past the shrunken watchdog.

    Only calls of the form ``git -C <repo> <subcommand> …`` hang, so git run elsewhere
    (for example a conftest teardown probing the checkout) is untouched."""
    real_git = shutil.which("git")
    assert real_git
    bindir = tmp_path / "bin"
    bindir.mkdir()
    wrapper = bindir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "-C" ] && [ "$2" = "{repo}" ] && [ "$3" = "$EEL_SLOW_SUBCMD" ]; then\n'
        "  exec sleep 30\n"
        "fi\n"
        f'exec "{real_git}" "$@"\n'
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(git_fetch, "_GIT_TIMEOUT", _BOUND_S)

    def _slow(subcommand: str) -> None:
        monkeypatch.setenv("EEL_SLOW_SUBCMD", subcommand)

    return _slow


def test_git_run_returns_the_shared_watchdog_outcome(repo, slow_git, monkeypatch):
    """git_run goes through gitutil.run_git_bounded, so is_watchdog_timeout recognises it."""
    calls: list[tuple[str, ...]] = []
    real = git_fetch.run_git_bounded

    def spy(cwd, *args, **kwargs):
        calls.append(args)
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(git_fetch, "run_git_bounded", spy)
    slow_git("rev-parse")
    proc = git_fetch.git_run(repo, "rev-parse", "HEAD")
    assert calls == [("rev-parse", "HEAD")]
    assert git_outcome.is_watchdog_timeout(proc)


def test_timed_out_rev_parse_is_not_reported_as_an_unresolvable_ref(repo, slow_git):
    slow_git("rev-parse")
    with pytest.raises(SnapshotError) as info:
        repo_snapshot.resolve_ref("HEAD", repo, fetch=False)
    assert not isinstance(info.value, SnapshotRefError)
    message = str(info.value)
    assert "git rev-parse" in message
    assert f"timed out after {_BOUND_S}s" in message
    assert "cannot resolve ref" not in message


def test_timed_out_git_remote_is_not_read_as_remote_absent(repo, slow_git):
    slow_git("remote")
    with pytest.raises(SnapshotError) as info:
        git_fetch.has_remote(repo)
    message = str(info.value)
    assert "git remote" in message
    assert f"timed out after {_BOUND_S}s" in message


def test_resolve_ref_fails_closed_when_git_remote_times_out(repo, slow_git, monkeypatch):
    """The fetch path is not silently skipped as though the remote were absent."""
    fetched: list[str | None] = []
    monkeypatch.setattr(repo_snapshot, "fetch_origin", lambda *a, **k: fetched.append(k.get("ref")))
    slow_git("remote")
    with pytest.raises(SnapshotError, match="git remote"):
        repo_snapshot.resolve_ref("HEAD", repo, fetch=True)
    assert fetched == []


def test_best_effort_blob_prefetch_stays_silent_on_a_remote_timeout(repo, slow_git, monkeypatch):
    """_ensure_blobs_present is documented best-effort: a timeout skips it, it never raises."""
    monkeypatch.setattr(repo_snapshot, "_has_missing_blobs", lambda *a, **k: True)
    slow_git("remote")
    head = subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert repo_snapshot._ensure_blobs_present(repo, head, "origin") is None


def test_healthy_calls_still_answer(repo, slow_git):
    """With no subcommand slowed, the remote is seen and HEAD resolves."""
    slow_git("no-such-subcommand")
    assert git_fetch.has_remote(repo) is True
    assert git_fetch.has_remote(repo, "upstream") is False
    assert len(repo_snapshot.resolve_ref("HEAD", repo, fetch=False)) in (40, 64)
    assert git_fetch.rev_parse(repo, "no-such-branch") is None
