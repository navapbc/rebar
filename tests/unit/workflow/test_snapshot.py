"""Unit tests for the hardened git-ref filesystem snapshot (WS-D2).

Builds real temp git repos and snapshots them; no LLM, no network.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from rebar.llm.workflow import snapshot as snap
from rebar.llm.workflow.snapshot import SnapshotError


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _repo(tmp_path: Path, files: dict[str, str], attrs: str | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@e.com", cwd=repo)
    _git("config", "user.name", "T", cwd=repo)
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    if attrs is not None:
        (repo / ".gitattributes").write_text(attrs)
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)
    return repo


def test_snapshot_extracts_tracked_tree_without_git(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"a.py": "print(1)\n", "pkg/b.py": "x = 2\n"})
    snapdir = snap.snapshot_at_ref("HEAD", str(repo))
    assert (snapdir / "a.py").read_text() == "print(1)\n"
    assert (snapdir / "pkg" / "b.py").read_text() == "x = 2\n"
    # No .git in the snapshot (git archive emits content only).
    assert not (snapdir / ".git").exists()


def test_snapshot_dir_is_named_for_the_sha(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"a.py": "1\n"})
    sha = snap.resolve_sha("HEAD", str(repo))
    snapdir = snap.snapshot_at_ref("HEAD", str(repo))
    assert snapdir.name == sha
    assert len(sha) == 40


def test_snapshot_is_read_only(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"a.py": "1\n"})
    snapdir = snap.snapshot_at_ref("HEAD", str(repo))
    f = snapdir / "a.py"
    assert not os.access(f, os.W_OK)


def test_snapshot_cache_by_sha(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"a.py": "1\n"})
    first = snap.snapshot_at_ref("HEAD", str(repo))
    second = snap.snapshot_at_ref("HEAD", str(repo))  # cache hit
    assert first == second
    assert first.is_dir()


def test_resolve_sha_bad_ref(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"a.py": "1\n"})
    with pytest.raises(SnapshotError, match="cannot resolve git ref"):
        snap.resolve_sha("no-such-ref", str(repo))


def test_size_guard_aborts(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"big.txt": "x" * 5000})
    with pytest.raises(SnapshotError, match="exceeds"):
        snap.snapshot_at_ref("HEAD", str(repo), max_bytes=100)


def test_export_ignore_is_respected(tmp_path: Path) -> None:
    # A path marked export-ignore in .gitattributes must be ABSENT from the
    # snapshot (git archive honors export-ignore natively).
    repo = _repo(
        tmp_path,
        {"keep.py": "1\n", "secret.env": "TOKEN=abc\n"},
        attrs="secret.env export-ignore\n",
    )
    snapdir = snap.snapshot_at_ref("HEAD", str(repo))
    assert (snapdir / "keep.py").exists()
    assert not (snapdir / "secret.env").exists()


def test_snapshot_at_specific_commit_is_immutable(tmp_path: Path) -> None:
    # Snapshotting an OLD commit reflects that commit, not the moving branch.
    repo = _repo(tmp_path, {"a.py": "v1\n"})
    old_sha = snap.resolve_sha("HEAD", str(repo))
    (repo / "a.py").write_text("v2\n")
    _git("commit", "-aqm", "v2", cwd=repo)
    old_snap = snap.snapshot_at_ref(old_sha, str(repo))
    head_snap = snap.snapshot_at_ref("HEAD", str(repo))
    assert (old_snap / "a.py").read_text() == "v1\n"
    assert (head_snap / "a.py").read_text() == "v2\n"
