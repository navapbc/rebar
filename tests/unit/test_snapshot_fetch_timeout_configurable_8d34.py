"""The snapshot fetch wall-clock timeout is a configurable backstop above the
former fixed 300-second limit. Low-speed settings detect a stalled transfer.
These tests inspect the timeout passed to the child and its resolver rather
than elapsed wall time.
"""

from __future__ import annotations

import subprocess

import pytest

from rebar import config
from rebar._snapshot import git_fetch


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return str(root)


def _capture_fetch_timeout(monkeypatch) -> list[float | None]:
    """Intercept the fetch child and record the ``timeout`` kwarg it was launched with."""
    seen: list[float | None] = []
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        if "fetch" not in argv:
            return real_run(argv, **kwargs)
        seen.append(kwargs.get("timeout"))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(git_fetch.subprocess, "run", fake_run)
    return seen


def test_default_fetch_ceiling_is_scaled_above_the_old_fixed_cap():
    """Out of the box the materialization fetch is bounded well above the old 300s cap."""
    assert git_fetch.fetch_timeout() > 300


def test_env_override_retunes_the_fetch_ceiling(monkeypatch):
    """A large/cold store can raise the ceiling via the owned config seam, live per call."""
    monkeypatch.setenv("REBAR_SNAPSHOT_FETCH_TIMEOUT_SECONDS", "5400")
    assert git_fetch.fetch_timeout() == 5400
    assert config.resolve_fetch_timeout(300) == 5400


def test_fetch_origin_uses_the_configured_ceiling_not_a_fixed_300(repo, tmp_path, monkeypatch):
    """The fetch child is launched with the CONFIGURED wall clock, not the hardcoded 300."""
    monkeypatch.setenv("REBAR_SNAPSHOT_FETCH_TIMEOUT_SECONDS", "1234")
    seen = _capture_fetch_timeout(monkeypatch)
    git_fetch.fetch_origin(repo, lock_path=tmp_path / "locks" / "fetch.lock")
    assert seen == [1234], seen


def test_fetch_origin_default_ceiling_exceeds_300(repo, tmp_path, monkeypatch):
    """With no override the child still gets a ceiling scaled above the old fixed cap."""
    monkeypatch.delenv("REBAR_SNAPSHOT_FETCH_TIMEOUT_SECONDS", raising=False)
    seen = _capture_fetch_timeout(monkeypatch)
    git_fetch.fetch_origin(repo, lock_path=tmp_path / "locks" / "fetch.lock")
    assert seen and seen[0] is not None and seen[0] > 300, seen


def test_timeout_error_reports_the_configured_ceiling(repo, tmp_path, monkeypatch):
    """A genuine backstop timeout fails closed naming the CONFIGURED ceiling (not 300)."""
    monkeypatch.setenv("REBAR_SNAPSHOT_FETCH_TIMEOUT_SECONDS", "777")
    monkeypatch.setenv("REBAR_SNAPSHOT_STALL_ATTEMPTS", "1")
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        if "fetch" not in argv:
            return real_run(argv, **kwargs)
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr(git_fetch.subprocess, "run", fake_run)
    with pytest.raises(git_fetch.SnapshotFetchError) as excinfo:
        git_fetch.fetch_origin(repo, lock_path=tmp_path / "locks" / "fetch.lock")
    message = str(excinfo.value)
    assert "777s" in message, message
    assert "300s" not in message, message


def test_blob_topup_refetch_uses_the_configured_ceiling(monkeypatch):
    """The OTHER materialization fetch — repo_snapshot's ``--refetch`` blob top-up — is
    bounded by the SAME configurable ceiling, not the old fixed 300s cap."""
    from rebar._snapshot import repo_snapshot as rs

    monkeypatch.setenv("REBAR_SNAPSHOT_FETCH_TIMEOUT_SECONDS", "2400")
    monkeypatch.setattr(rs, "_has_missing_blobs", lambda *a, **k: True)
    monkeypatch.setattr(rs, "has_remote", lambda *a, **k: True)
    seen: list[float | None] = []
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        if "fetch" not in argv:
            return real_run(argv, **kwargs)
        seen.append(kwargs.get("timeout"))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(rs.subprocess, "run", fake_run)
    rs._ensure_blobs_present("/nonexistent", "0" * 40, "origin")
    assert seen == [2400], seen
