"""Bound init's cold tickets fetch and fsck repair's warm push (983f).

The cold fetch uses the tunable fetch_timeout backstop. Incremental repair
uses the store's shorter write bound. Helpers pass timeout and convert
TimeoutExpired to a failed CompletedProcess with operation and bound in stderr."""

from __future__ import annotations

import subprocess

import pytest

from rebar._commands import fsck_repair, init
from rebar._snapshot import repo_snapshot as _snap
from rebar._store import push as _push

pytestmark = pytest.mark.unit


# ── The bound each site chose matches the cited precedent ─────────────────────────────


def test_init_fetch_timeout_matches_cold_snapshot_precedent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # init's cold tickets fetch and the snapshot cold-materialize fetch share the SAME
    # tunable backstop, scaled above the old fixed 300s cap (bug curly-open-swan), and both
    # honour the same env override live per call.
    monkeypatch.delenv("REBAR_SNAPSHOT_FETCH_TIMEOUT_SECONDS", raising=False)
    assert init._fetch_timeout() == _snap.fetch_timeout()
    assert init._fetch_timeout() > 300
    monkeypatch.setenv("REBAR_SNAPSHOT_FETCH_TIMEOUT_SECONDS", "4200")
    assert init._fetch_timeout() == _snap.fetch_timeout() == 4200


def test_fsck_repair_push_timeout_matches_store_precedent() -> None:
    assert fsck_repair._PUSH_TIMEOUT == _push._GIT_TIMEOUT == 30


# ── The timeout is actually passed to the git child ───────────────────────────────────


def test_init_fetch_passes_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def _spy(cwd, *args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr(init, "run_git", _spy)
    init._git_fetch("/nonexistent", "fetch", "origin", "tickets")
    assert seen["timeout"] == init._fetch_timeout()
    assert seen["timeout"] is not None and seen["timeout"] > 0


def test_fsck_repair_push_passes_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def _spy(cwd, *args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr(fsck_repair, "run_git", _spy)
    fsck_repair._git_push("/nonexistent", "push", "origin", "HEAD:tickets")
    assert seen["timeout"] == fsck_repair._PUSH_TIMEOUT
    assert seen["timeout"] is not None and seen["timeout"] > 0


# ── A timing-out call folds into a descriptive 124, not a bare TimeoutExpired ──────────


def test_init_fetch_folds_timeout_into_descriptive_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REBAR_SNAPSHOT_FETCH_TIMEOUT_SECONDS", "1500")

    def _boom(cwd, *args, **kwargs):
        raise subprocess.TimeoutExpired(["git", *args], kwargs.get("timeout") or 1)

    monkeypatch.setattr(init, "run_git", _boom)
    result = init._git_fetch("/nonexistent", "fetch", "origin", "tickets")
    assert result.returncode == 124
    assert "timed out after 1500s" in (result.stderr or "")
    assert "fetch" in (result.stderr or ""), "the timeout error must name the git op"


def test_fsck_repair_push_folds_timeout_into_descriptive_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(cwd, *args, **kwargs):
        raise subprocess.TimeoutExpired(["git", *args], kwargs.get("timeout") or 1)

    monkeypatch.setattr(fsck_repair, "run_git", _boom)
    result = fsck_repair._git_push("/nonexistent", "push", "origin", "HEAD:tickets")
    assert result.returncode == 124
    assert "timed out after 30s" in (result.stderr or "")
    assert "push" in (result.stderr or ""), "the timeout error must name the git op"


# ── Wiring: the call sites route through the bounded helper (not the unbounded _git) ───


def test_init_remote_mount_uses_the_bounded_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The remote-branch arm of ``_mount_or_create_branch`` must fetch via ``_git_fetch``
    (bounded) — a regression back to the unbounded ``_git`` would skip this spy."""
    calls: list = []

    def _fetch_spy(cwd, *args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    # No local branch, a present remote-tracking ref, and a stubbed worktree add so the
    # function reaches (and only reaches) the remote arm.
    monkeypatch.setattr(init, "_git_fetch", _fetch_spy)
    monkeypatch.setattr(
        init,
        "_git_ok",
        lambda cwd, *a: a[:2] == ("rev-parse", "--verify") and a[2].startswith("origin/"),
    )
    monkeypatch.setattr(
        init, "_git", lambda cwd, *a: subprocess.CompletedProcess(["git", *a], 0, "", "")
    )
    monkeypatch.setattr(init, "_ensure_branch_user_config", lambda repo, tracker: None)
    import rebar.config as _cfg

    monkeypatch.setattr(_cfg, "tickets_branch", lambda repo: "tickets")
    monkeypatch.setattr(_cfg, "tickets_remote", lambda repo: "origin")

    rc = init._mount_or_create_branch("/nonexistent-repo", "/nonexistent-tracker")
    assert rc == 0
    assert any(a[0] == "fetch" for a in calls), (
        "the remote-branch mount did not fetch through the bounded _git_fetch helper"
    )
