"""The materialization fetch's wall-clock ceiling is configurable/scaled, not a fixed 300s.

Ticket ``8d34`` (curly-open-swan). The completion-verifier's repo-materialization fetch was
bounded by a HARD ``_GIT_TIMEOUT = 300`` wall clock. On a large/cold store an HONEST
``--no-filter`` transfer legitimately runs longer than 300s while staying ABOVE the low-speed
floor, so the throughput-keyed stall-abort never fires — yet the fixed wall clock cuts it off
and the verifier fails closed verdict-less.

The fix keeps the stall-abort as the real guard against a wedged remote and turns the
wall-clock into a GENEROUS, TUNABLE backstop: ``REBAR_SNAPSHOT_FETCH_TIMEOUT_SECONDS`` over a
default scaled well above 300. These tests assert the effective ``timeout`` handed to the
child and the resolver, NEVER elapsed time (a wall-clock assertion would flake and would still
pass under the buggy fixed cap). They are distinct from the stall-abort tests in
``test_snapshot_fetch_stall_abort_12e4.py``.
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
