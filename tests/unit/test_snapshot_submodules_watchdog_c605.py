"""A failed or timed-out submodule listing is never persisted as "no submodules".

Bug reluctant-corrupt-whippet: ``repo_snapshot._list_submodules`` parsed the stdout of
``git ls-tree`` without checking how the call ended. A watchdog-killed or failed listing has
empty stdout, so it answered "no submodules", and ``materialize`` wrote that answer to the
immutable ``<sha>.caveats.json`` sidecar, where every later cache hit trusted it. These tests
fire the real watchdog the way ``test_snapshot_git_watchdog_d33f`` does: a ``git`` wrapper
on ``PATH`` replaces ``ls-tree`` with ``exec sleep`` and ``_GIT_TIMEOUT`` is shrunk to 1 s
while the hang is switched on.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from rebar._snapshot import git_fetch, repo_snapshot
from rebar._snapshot.git_fetch import SnapshotError

_BOUND_S = 1


def _git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """A repo whose HEAD tree holds one gitlink (``vendor/sub``); returns (root, sha).

    The snapshot store is pointed at a per-test directory."""
    store = tmp_path / "gate-tmpdir"
    store.mkdir()
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(store))
    root = str(tmp_path / "repo")
    subprocess.run(["git", "init", "-q", root], check=True)
    _git(root, "config", "user.email", "t@e")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    Path(root, "top.txt").write_text("top\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    _git(root, "update-index", "--add", "--cacheinfo", f"160000,{base},vendor/sub")
    tree = _git(root, "write-tree")
    sha = _git(root, "commit-tree", tree, "-m", "gitlink", "-p", base)
    _git(root, "reset", "--soft", sha)
    return root, sha


@pytest.fixture
def slow_ls_tree(repo: tuple[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Return a switch that makes ``git -C <repo> ls-tree`` hang past a 1 s watchdog."""
    root, _sha = repo
    real_git = shutil.which("git")
    assert real_git
    bindir = tmp_path / "bin"
    bindir.mkdir()
    wrapper = bindir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'if [ -n "$WHIPPET_SLOW" ] && [ "$1" = "-C" ] && [ "$2" = "{root}" ] '
        '&& [ "$3" = "ls-tree" ]; then\n'
        "  exec sleep 30\n"
        "fi\n"
        # A loaded host: the recovery listing answers, just later than the 1 s slow-path bound.
        f'if [ -n "$WHIPPET_LOADED" ] && [ "$1" = "-C" ] && [ "$2" = "{root}" ] '
        '&& [ "$3" = "ls-tree" ]; then\n'
        "  sleep 2\n"
        "fi\n"
        f'exec "{real_git}" "$@"\n'
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    real_bound = git_fetch._GIT_TIMEOUT

    # The 1 s bound applies only while the hang is switched on, so the recovery call that
    # follows runs with the production bound and cannot race host load (civil-fameless-starfish).
    def _set(on: bool) -> None:
        if on:
            monkeypatch.setenv("WHIPPET_SLOW", "1")
            monkeypatch.setattr(git_fetch, "_GIT_TIMEOUT", _BOUND_S)
        else:
            monkeypatch.delenv("WHIPPET_SLOW", raising=False)
            monkeypatch.setattr(git_fetch, "_GIT_TIMEOUT", real_bound)

    return _set


def _sidecar(sha: str) -> Path:
    return repo_snapshot.store_root() / f"{sha}.caveats.json"


def test_timed_out_listing_raises_instead_of_answering_none(repo, slow_ls_tree):
    root, sha = repo
    slow_ls_tree(True)
    with pytest.raises(SnapshotError) as info:
        repo_snapshot._list_submodules(root, sha)
    assert "ls-tree" in str(info.value)
    assert f"{_BOUND_S}s" in str(info.value)


def test_failed_listing_raises_instead_of_answering_none(repo):
    root, _sha = repo
    with pytest.raises(SnapshotError) as info:
        repo_snapshot._list_submodules(root, "0" * 40)
    assert "ls-tree" in str(info.value)


def test_build_with_timed_out_listing_writes_no_sidecar(repo, slow_ls_tree):
    """Cache miss: the entry is built, the listing times out, nothing is persisted."""
    root, sha = repo
    slow_ls_tree(True)
    with pytest.raises(SnapshotError):
        repo_snapshot.materialize(sha, repo_root=root, fetch=False)
    assert not _sidecar(sha).exists()

    # Once git answers, the next call recomputes and records the real gitlink.
    slow_ls_tree(False)
    handle = repo_snapshot.materialize(sha, repo_root=root, fetch=False)
    assert handle.submodules == ("vendor/sub",)
    assert '"vendor/sub"' in _sidecar(sha).read_text()


def test_cache_hit_with_timed_out_listing_writes_no_sidecar(repo, slow_ls_tree):
    """Cache hit without a sidecar: a timed-out recompute is not persisted either."""
    root, sha = repo
    repo_snapshot.materialize(sha, repo_root=root, fetch=False)
    _sidecar(sha).unlink()

    slow_ls_tree(True)
    with pytest.raises(SnapshotError):
        repo_snapshot.materialize(sha, repo_root=root, fetch=False)
    assert not _sidecar(sha).exists()


def test_healthy_listing_still_answers(repo):
    root, sha = repo
    assert repo_snapshot._list_submodules(root, sha) == ("vendor/sub",)
    base = _git(root, "rev-parse", "HEAD~1")
    assert repo_snapshot._list_submodules(root, base) == ()


def test_recovery_listing_is_not_bound_by_the_slow_path_budget(
    repo, slow_ls_tree, monkeypatch: pytest.MonkeyPatch
):
    """civil-fameless-starfish: the 1 s bound must apply only to the deliberately slow call.

    The recovery ``materialize`` once inherited the 1 s budget for the whole test, so a loaded
    host (here: an ``ls-tree`` that answers after 2 s) failed the call that is meant to pass."""
    root, sha = repo
    slow_ls_tree(True)
    with pytest.raises(SnapshotError):
        repo_snapshot.materialize(sha, repo_root=root, fetch=False)

    slow_ls_tree(False)
    monkeypatch.setenv("WHIPPET_LOADED", "1")
    handle = repo_snapshot.materialize(sha, repo_root=root, fetch=False)
    assert handle.submodules == ("vendor/sub",)
