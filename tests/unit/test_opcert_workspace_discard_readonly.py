"""Op-cert job workspaces are removed even when git left read-only files (bug
inaccurate-genetic-asianlion).

Git writes its objects read-only. Windows refuses to delete a read-only file, and
``discard`` swallowed that, so every op-cert job on Windows leaked its workspace. These tests
reproduce the Windows refusal on any platform with a fake ``os.unlink`` that refuses a target
lacking ``S_IWRITE``; real directory permissions stay writable.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from rebar.opcert_service import workspace

pytestmark = pytest.mark.unit

_real_unlink = os.unlink


def _git_like_tree(root: Path) -> Path:
    objects = root / ".git" / "objects" / "4f"
    objects.mkdir(parents=True)
    for name in ("0773921a7ea8db48f3103a1f85037e1fe7ded5", "1ee7e606772999964a5477e125f293"):
        blob = objects / name
        blob.write_bytes(b"x")
        blob.chmod(stat.S_IREAD)
    (root / "README").write_text("ws", encoding="utf-8")
    return root


def test_discard_removes_read_only_files_under_windows_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _git_like_tree(tmp_path / "rebar-opcert-ws-x")
    refusals: list[str] = []

    def windows_unlink(path, *, dir_fd=None):
        if not os.stat(path, dir_fd=dir_fd).st_mode & stat.S_IWRITE:
            refusals.append(os.fspath(path))
            raise PermissionError(13, "Access is denied", os.fspath(path))
        _real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", windows_unlink)
    workspace.discard(str(root))

    assert refusals, "the fake never refused, so the test did not reproduce Windows"
    assert not root.exists()


def test_discard_never_raises_when_removal_keeps_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _git_like_tree(tmp_path / "rebar-opcert-ws-y")

    def always_denied(path, *, dir_fd=None):
        raise PermissionError(13, "Access is denied", os.fspath(path))

    monkeypatch.setattr(os, "unlink", always_denied)
    workspace.discard(str(root))  # must not raise
    monkeypatch.undo()
    for blob in root.rglob("*"):
        if blob.is_file():
            blob.chmod(stat.S_IWRITE | stat.S_IREAD)


def test_discard_of_a_missing_path_is_a_no_op(tmp_path: Path) -> None:
    workspace.discard(str(tmp_path / "never-created"))


@pytest.mark.parametrize("func", [os.open, os.scandir], ids=["open", "scandir"])
def test_the_hook_ignores_calls_it_cannot_retry(tmp_path: Path, func) -> None:
    workspace._retry_writable(func, str(tmp_path / "gone"), None)
