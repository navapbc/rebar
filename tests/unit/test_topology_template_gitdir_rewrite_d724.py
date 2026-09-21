"""The linked-worktree ``.git`` retry must rewrite in place, never recreate.

Windows denies ``CREATE_ALWAYS`` -- which is what ``open("wb")`` maps to -- on a file
carrying ``FILE_ATTRIBUTE_HIDDEN``, and Git for Windows marks a linked worktree's ``.git``
pointer hidden. ``os.chmod`` only toggles the read-only attribute there, so clearing the
read-only bit does not lift the refusal. The observed CI failure showed exactly that: the
first ``write_bytes`` raised ``PermissionError``, the guard matched, ``chmod`` ran, and the
retry raised the identical error.

POSIX cannot reproduce the attribute itself, so these tests pin the mechanism that the fix
depends on: after the guard chmods, the retry must open the EXISTING file rather than ask
for a creating mode.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from _topology_template import _write_rewritten_path

_ORIGINAL = b"gitdir: /old/path/.git/worktrees/-tickets-tracker\n"
_REWRITTEN = b"gitdir: /new/path/.git/worktrees/-tickets-tracker\n"


def _marker(tmp_path: Path) -> Path:
    path = tmp_path / ".git"
    path.write_bytes(_ORIGINAL)
    return path


def _deny_write_bytes(monkeypatch: pytest.MonkeyPatch, target: Path) -> list[Path]:
    """Make ``Path.write_bytes`` raise for ``target``, recording every attempt.

    This stands in for the Windows refusal, which ``chmod`` cannot clear: unlike a plain
    read-only bit, it denies EVERY creating open, including the one after the chmod.
    """
    attempts: list[Path] = []
    real = Path.write_bytes

    def fake(self: Path, data: bytes) -> int:
        if self == target:
            attempts.append(self)
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", fake)
    return attempts


def test_retry_rewrites_in_place_when_recreating_stays_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _marker(tmp_path)
    attempts = _deny_write_bytes(monkeypatch, path)

    _write_rewritten_path(path, _REWRITTEN, original=_ORIGINAL)

    assert path.read_bytes() == _REWRITTEN
    assert len(attempts) == 1, (
        "the retry must not reach write_bytes again -- a second creating open is exactly "
        "what Windows refuses on the hidden .git pointer"
    )


def test_retry_restores_the_original_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _marker(tmp_path)
    path.chmod(0o444)
    original_mode = path.stat().st_mode
    _deny_write_bytes(monkeypatch, path)

    _write_rewritten_path(path, _REWRITTEN, original=_ORIGINAL)

    assert path.stat().st_mode == original_mode
    assert not path.stat().st_mode & stat.S_IWUSR


def test_truncation_leaves_no_trailing_bytes_from_a_longer_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-place write must truncate; ``CREATE_ALWAYS`` truncated for free."""
    longer = b"gitdir: /a/much/longer/original/path/.git/worktrees/-tickets-tracker\n"
    path = tmp_path / ".git"
    path.write_bytes(longer)
    _deny_write_bytes(monkeypatch, path)

    _write_rewritten_path(path, _REWRITTEN, original=longer)

    assert path.read_bytes() == _REWRITTEN


def test_non_gitdir_permission_error_still_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard is unchanged: only a ``gitdir:`` ``.git`` marker gets the retry."""
    path = tmp_path / "config"
    path.write_bytes(b"[core]\n")
    _deny_write_bytes(monkeypatch, path)

    with pytest.raises(PermissionError):
        _write_rewritten_path(path, b"[core]\n\tbare = false\n", original=b"[core]\n")
