"""``fsutil.replace_with_retry``: a contended Windows replace is retried, nothing else is.

On Windows ``os.replace`` fails with ``PermissionError`` while another rename onto the
same target is in flight or another handle holds it (bug sporting-feudalist-bluet). These
tests drive both platform branches on any host by patching the module's platform switch
and its ``os.replace`` seam, so the Windows branch is pinned on POSIX CI too.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rebar._store import fsutil

pytestmark = pytest.mark.unit


def _scripted_replace(monkeypatch: pytest.MonkeyPatch, failures: list[BaseException]) -> list:
    """Make ``os.replace`` raise each queued error once, then really replace."""
    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def replace(src, dst):
        calls.append((os.fspath(src), os.fspath(dst)))
        if failures:
            raise failures.pop(0)
        return real_replace(src, dst)

    monkeypatch.setattr(fsutil.os, "replace", replace)
    monkeypatch.setattr(fsutil.time, "sleep", lambda _s: None)
    return calls


def _src_dst(tmp_path: Path) -> tuple[Path, Path]:
    src = tmp_path / "src.tmp"
    src.write_text("new", encoding="utf-8")
    dst = tmp_path / "target"
    dst.write_text("old", encoding="utf-8")
    return src, dst


def test_windows_retries_a_contended_replace_until_it_publishes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(fsutil, "_RETRY_CONTENDED_REPLACE", True)
    src, dst = _src_dst(tmp_path)
    calls = _scripted_replace(
        monkeypatch,
        [PermissionError(13, "Access is denied"), PermissionError(13, "Access is denied")],
    )

    fsutil.replace_with_retry(src, dst)

    assert len(calls) == 3
    assert dst.read_text(encoding="utf-8") == "new"
    assert not src.exists()


def test_windows_reraises_once_the_retry_window_has_passed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(fsutil, "_RETRY_CONTENDED_REPLACE", True)
    monkeypatch.setattr(fsutil, "_REPLACE_RETRY_DEADLINE_S", 0.0)
    src, dst = _src_dst(tmp_path)
    calls = _scripted_replace(monkeypatch, [PermissionError(13, "Access is denied")] * 5)

    with pytest.raises(PermissionError):
        fsutil.replace_with_retry(src, dst)

    assert len(calls) == 1, "a zero window must not retry"
    assert dst.read_text(encoding="utf-8") == "old", "the target is untouched on failure"


def test_windows_never_retries_a_non_permission_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(fsutil, "_RETRY_CONTENDED_REPLACE", True)
    src, dst = _src_dst(tmp_path)
    calls = _scripted_replace(monkeypatch, [FileNotFoundError(2, "gone")])

    with pytest.raises(FileNotFoundError):
        fsutil.replace_with_retry(src, dst)

    assert len(calls) == 1


def test_posix_propagates_permission_error_without_retrying(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(fsutil, "_RETRY_CONTENDED_REPLACE", False)
    src, dst = _src_dst(tmp_path)
    calls = _scripted_replace(monkeypatch, [PermissionError(13, "Permission denied")])

    with pytest.raises(PermissionError):
        fsutil.replace_with_retry(src, dst)

    assert len(calls) == 1


def test_atomic_write_publishes_through_the_retrying_replace(tmp_path, monkeypatch) -> None:
    """The shared write path is the class-level fix: one transient Windows refusal must
    not drop the write."""
    monkeypatch.setattr(fsutil, "_RETRY_CONTENDED_REPLACE", True)
    calls = _scripted_replace(monkeypatch, [PermissionError(13, "Access is denied")])
    target = tmp_path / "cursor"

    fsutil.atomic_write(target, "200")

    assert len(calls) == 2
    assert target.read_text(encoding="utf-8") == "200"
    assert [p.name for p in tmp_path.iterdir()] == ["cursor"], "no temp residue"


def test_the_platform_switch_matches_the_running_platform() -> None:
    assert fsutil._RETRY_CONTENDED_REPLACE is (os.name == "nt")
