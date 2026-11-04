"""A file copy of a linked worktree must not be written through (bug asthmatic-grave-caudata).

``cp -R`` of a linked worktree keeps the original's ``.git`` pointer file, so git in the copy
acts on the original repository: its config, its branch refs, and the original worktree's own
index and HEAD. git records where the worktree really lives in ``<gitdir>/gitdir``; a copy is
the directory whose path disagrees with that back-pointer.

The detector tests drive :meth:`StorePaths.copied_worktree_origin` on real git worktrees. The
end-to-end test runs ``rebar create`` against a copied tracker and asserts the shared
repository is untouched, which is the damage the bug did on 2026-10-27.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

import rebar
from rebar._store import compat
from rebar._store.paths import StorePaths


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        env=subprocess_env(),
    )


def _repo_with_worktree(root: Path, *config: str) -> tuple[Path, Path]:
    """A repository at ``root/main`` with a linked worktree of ``side`` at ``root/wt``."""
    main = root / "main"
    main.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=main)
    _git(
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=T",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
        cwd=main,
    )
    _git("branch", "side", cwd=main)
    _git(*config, "worktree", "add", "-q", str(root / "wt"), "side", cwd=main)
    return main, root / "wt"


def _real(tmp_path: Path) -> Path:
    # macOS hands out /var/... while git records /private/var/...; start from the real path
    # so a pass cannot depend on the detector happening to resolve symlinks.
    return Path(os.path.realpath(tmp_path))


# ── detector ──────────────────────────────────────────────────────────────────
def test_original_linked_worktree_is_not_a_copy(tmp_path: Path) -> None:
    _, wt = _repo_with_worktree(_real(tmp_path))
    assert StorePaths(str(wt)).copied_worktree_origin() is None


def test_file_copy_of_linked_worktree_names_the_original(tmp_path: Path) -> None:
    root = _real(tmp_path)
    _, wt = _repo_with_worktree(root)
    copy = root / "copy"
    shutil.copytree(wt, copy, symlinks=True)
    assert StorePaths(str(copy)).copied_worktree_origin() == str(wt)


def test_original_reached_through_a_symlink_is_not_a_copy(tmp_path: Path) -> None:
    # A rebar worktree reaches the canonical tracker through a .tickets-tracker symlink.
    root = _real(tmp_path)
    _, wt = _repo_with_worktree(root)
    link = root / "view"
    link.symlink_to(wt)
    assert StorePaths(str(link)).copied_worktree_origin() is None


def test_ordinary_clone_is_not_a_copy(tmp_path: Path) -> None:
    root = _real(tmp_path)
    main, _ = _repo_with_worktree(root)
    copy = root / "main-copy"
    shutil.copytree(main, copy, symlinks=True)  # .git is a directory: an independent repo
    assert StorePaths(str(main)).copied_worktree_origin() is None
    assert StorePaths(str(copy)).copied_worktree_origin() is None


def test_gitdir_without_back_pointer_is_not_judged(tmp_path: Path) -> None:
    # A submodule's .git file points at <super>/.git/modules/<name>, which has no gitdir
    # back-pointer; nothing can be compared, so the detector stays out of the way.
    root = _real(tmp_path)
    modules = root / "super.git" / "modules" / "sub"
    modules.mkdir(parents=True)
    sub = root / "sub"
    sub.mkdir()
    (sub / ".git").write_text(f"gitdir: {modules}\n", encoding="utf-8")
    assert StorePaths(str(sub)).copied_worktree_origin() is None


def test_relative_path_worktree_copy_is_detected(tmp_path: Path) -> None:
    root = _real(tmp_path)
    try:
        _, wt = _repo_with_worktree(root, "-c", "worktree.useRelativePaths=true")
    except subprocess.CalledProcessError:
        pytest.skip("this git cannot create relative-path worktrees")
    if not (wt / ".git").read_text(encoding="utf-8").startswith("gitdir: .."):
        pytest.skip("this git ignored worktree.useRelativePaths")
    assert StorePaths(str(wt)).copied_worktree_origin() is None
    copy = root / "copy"  # a sibling, so the relative pointer still reaches the real gitdir
    shutil.copytree(wt, copy, symlinks=True)
    assert StorePaths(str(copy)).copied_worktree_origin() == str(wt)


def test_check_store_compat_refuses_a_copy(tmp_path: Path) -> None:
    root = _real(tmp_path)
    _, wt = _repo_with_worktree(root)
    copy = root / "copy"
    shutil.copytree(wt, copy, symlinks=True)
    compat.check_store_compat(str(wt))  # the original stays writable
    with pytest.raises(compat.CopiedWorktreeError) as excinfo:
        compat.check_store_compat(str(copy))
    assert isinstance(excinfo.value, compat.StoreIncompatibleError)
    assert str(wt) in str(excinfo.value)


def test_refusal_offers_an_isolated_clone(tmp_path: Path) -> None:
    root = _real(tmp_path)
    _, wt = _repo_with_worktree(root)
    copy = root / "copy"
    shutil.copytree(wt, copy, symlinks=True)
    with pytest.raises(compat.CopiedWorktreeError) as excinfo:
        compat.check_store_compat(str(copy))
    assert "git clone --local --branch tickets" in str(excinfo.value)


def test_moved_worktree_is_not_a_copy(tmp_path: Path) -> None:
    # A relocated tracker (shutil.move / mv) leaves git's back-pointer stale, but nothing
    # else shares the git dir any more, so it is the supported relocated store.
    root = _real(tmp_path)
    _, wt = _repo_with_worktree(root)
    moved = root / "moved"
    shutil.move(str(wt), moved)
    assert StorePaths(str(moved)).copied_worktree_origin() is None
    compat.check_store_compat(str(moved))


def test_copy_whose_original_now_uses_another_gitdir_is_not_judged(tmp_path: Path) -> None:
    # The recorded directory exists but is no longer the worktree this gitdir serves.
    root = _real(tmp_path)
    _, wt = _repo_with_worktree(root)
    copy = root / "copy"
    shutil.copytree(wt, copy, symlinks=True)
    (wt / ".git").write_text(f"gitdir: {root / 'elsewhere'}\n", encoding="utf-8")
    assert StorePaths(str(copy)).copied_worktree_origin() is None


# ── end to end ────────────────────────────────────────────────────────────────
@pytest.fixture
def rebar_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A rebar-initialized repo whose tracker is a linked worktree, with two remotes."""
    repo = _real(tmp_path) / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    rebar.init_repo(repo_root=str(repo))
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    _git("remote", "add", "origin", str(repo.parent / "origin.git"), cwd=repo)
    _git("remote", "add", "gerrit", str(repo.parent / "gerrit.git"), cwd=repo)
    return repo


def _shared_state(repo: Path) -> tuple[bytes, str]:
    refs = _git("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", cwd=repo)
    return (repo / ".git" / "config").read_bytes(), refs.stdout


def _create(repo: Path, tracker: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", "create", "task", "written through a copy"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        env=subprocess_env(REBAR_ROOT=str(repo), REBAR_TRACKER_DIR=str(tracker)),
    )


def test_rebar_write_in_a_copied_tracker_leaves_the_shared_repo_unchanged(
    rebar_repo: Path,
) -> None:
    tracker = rebar_repo / ".tickets-tracker"
    copy = rebar_repo.parent / "tracker-copy"
    shutil.copytree(tracker, copy, symlinks=True)
    before = _shared_state(rebar_repo)

    result = _create(rebar_repo, copy)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "copy" in result.stderr
    assert str(tracker) in result.stderr
    assert _shared_state(rebar_repo) == before


def test_rebar_write_in_the_original_tracker_still_succeeds(rebar_repo: Path) -> None:
    tracker = rebar_repo / ".tickets-tracker"
    before_refs = _shared_state(rebar_repo)[1]

    result = _create(rebar_repo, tracker)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _shared_state(rebar_repo)[1] != before_refs  # the tickets branch advanced
