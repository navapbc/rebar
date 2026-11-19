"""The POSIX mode-bit checks run only where the platform has mode bits.

Windows reports every file as 0o666 and every directory as 0o777, so the op-cert key check
(``keyprov``) and the comment-echo cleanup artifact-directory check always refused there.
By operator decision (cloistered-decomposed-nymph, option 3) Windows relies on the user
profile's default ACL instead (bug almondy-columned-iguanodon). Each module's
``_POSIX_MODE_BITS`` constant selects the branch, so both branches run on every platform.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from rebar.opcert_service import keyprov

_SCRIPTS = Path(__file__).resolve().parents[2] / "infra" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import cleanup_comment_echo_jira as cleanup  # noqa: E402

pytestmark = pytest.mark.unit

_POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="chmod cannot set group/other bits on Windows"
)


def _loose_key(tmp_path: Path) -> Path:
    key = tmp_path / "loosekey"
    key.write_bytes(b"not parsed by the mode check\n")
    key.chmod(0o644)
    return key


def _loose_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "artifacts"
    directory.mkdir()
    directory.chmod(0o755)
    return directory


def test_the_switches_follow_the_running_platform() -> None:
    assert keyprov._POSIX_MODE_BITS is (os.name != "nt")
    assert cleanup._POSIX_MODE_BITS is (os.name != "nt")


@_POSIX_ONLY
def test_a_loose_key_is_rejected_where_mode_bits_exist(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(keyprov, "_POSIX_MODE_BITS", True)
    with pytest.raises(keyprov.OpcertKeyError, match="0600 or stricter"):
        keyprov._read_path_source(str(_loose_key(tmp_path)))


def test_a_loose_key_is_read_where_mode_bits_do_not_exist(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(keyprov, "_POSIX_MODE_BITS", False)
    assert keyprov._read_path_source(str(_loose_key(tmp_path))) == (
        b"not parsed by the mode check\n"
    )


@pytest.mark.parametrize("mode_bits", [True, False])
def test_a_non_regular_key_path_is_rejected_either_way(tmp_path, monkeypatch, mode_bits) -> None:
    monkeypatch.setattr(keyprov, "_POSIX_MODE_BITS", mode_bits)
    with pytest.raises(keyprov.OpcertKeyError, match="not a regular file"):
        keyprov._read_path_source(str(tmp_path))


@_POSIX_ONLY
def test_a_loose_artifact_dir_is_rejected_where_mode_bits_exist(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cleanup, "_POSIX_MODE_BITS", True)
    with pytest.raises(cleanup.CleanupError, match="group/world accessible"):
        cleanup._assert_private_artifact_dir(_loose_dir(tmp_path))


def test_a_loose_artifact_dir_is_accepted_where_mode_bits_do_not_exist(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(cleanup, "_POSIX_MODE_BITS", False)
    cleanup._assert_private_artifact_dir(_loose_dir(tmp_path))


@pytest.mark.parametrize("mode_bits", [True, False])
def test_a_file_is_not_an_artifact_dir_either_way(tmp_path, monkeypatch, mode_bits) -> None:
    monkeypatch.setattr(cleanup, "_POSIX_MODE_BITS", mode_bits)
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x", encoding="utf-8")
    with pytest.raises(cleanup.CleanupError, match="real directory"):
        cleanup._assert_private_artifact_dir(not_a_dir)


def _symlinked_artifact(tmp_path: Path) -> Path:
    target = tmp_path / "real.json"
    target.write_text("{}\n", encoding="utf-8")
    link = tmp_path / "jira-before.json"
    try:
        link.symlink_to(target)
    except OSError as exc:  # Windows without the symlink privilege
        pytest.skip(f"cannot create a symlink here: {exc}")
    return link


def test_a_symlinked_artifact_is_refused_without_o_nofollow(tmp_path, monkeypatch) -> None:
    """Windows has no O_NOFOLLOW, so os.open would follow the link to a regular file. The
    explicit is_symlink() refusal must hold on its own."""
    link = _symlinked_artifact(tmp_path)
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    with pytest.raises(cleanup.CleanupError, match="must be a regular file"):
        cleanup._read_regular(link, "jira-before")


def test_a_symlinked_journal_is_refused_without_o_nofollow(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cleanup, "_POSIX_MODE_BITS", False)
    link = _symlinked_artifact(tmp_path)
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    with pytest.raises(cleanup.CleanupError, match="journal must be a regular file"):
        cleanup._append_record(link, {"event": "x"})
    assert (tmp_path / "real.json").read_text(encoding="utf-8") == "{}\n"


def test_a_symlink_is_never_overwritten_as_a_new_artifact(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cleanup, "_POSIX_MODE_BITS", False)
    link = _symlinked_artifact(tmp_path)
    (tmp_path / "real.json").unlink()  # dangling: O_EXCL alone would not see an existing file
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    with pytest.raises(cleanup.CleanupError, match="refusing to overwrite"):
        cleanup._write_new(link, {"event": "x"})
    assert not (tmp_path / "real.json").exists()
