"""WS2: the write-path pending-hint (``ensures.maybe_emit_pending_hint``).

A store that is behind the ensure registry should nudge ``rebar fsck --repair`` on
write activity — cheaply (≤1 marker read/process), rate-limited via ``.ensure-hinted``,
suppressible via ``[ensure]`` config, and NEVER breaking a write. These tests assert
on captured log output and on the read-dedup + fail-silent contract.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import config
from rebar._store import ensures


@pytest.fixture
def fresh_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "base"], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    return repo


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    """Isolate the per-process pending cache + config memo between tests."""
    ensures._reset_pending_cache()
    config.reset_config_cache()


def _tracker(repo: Path) -> Path:
    return repo / ".tickets-tracker"


def _make_pending(tracker: Path) -> None:
    """Force a pending store: drop the marker WS1 wrote and reset the cache so the
    next hint call recomputes pending = all registry ids."""
    (tracker / ensures.APPLIED_MARKER).unlink(missing_ok=True)
    (tracker / ensures.HINTED_MARKER).unlink(missing_ok=True)
    ensures._reset_pending_cache()


# ── (a) pending store emits exactly one WARNING, re-emits only after interval ──
def test_pending_store_emits_one_warning_then_rate_limited(
    fresh_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rebar.init_repo(repo_root=str(fresh_repo))
    tracker = _tracker(fresh_repo)
    _make_pending(tracker)

    with caplog.at_level(logging.WARNING, logger="rebar"):
        ensures.maybe_emit_pending_hint(tracker)
    warnings = [r for r in caplog.records if "pending" in r.getMessage()]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "rebar fsck --repair" in msg
    # names pending units (all five on a freshly-cleared marker)
    assert "gc-config" in msg and "gitignore" in msg
    assert (tracker / ensures.HINTED_MARKER).is_file()

    # A second write within the interval is rate-limited — no new warning.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="rebar"):
        ensures.maybe_emit_pending_hint(tracker)
    assert [r for r in caplog.records if "pending" in r.getMessage()] == []


def test_pending_store_reemits_after_interval(
    fresh_repo: Path, caplog: pytest.LogCaptureFixture, monkeypatch
) -> None:
    rebar.init_repo(repo_root=str(fresh_repo))
    tracker = _tracker(fresh_repo)
    _make_pending(tracker)

    ensures.maybe_emit_pending_hint(tracker)  # stamps .ensure-hinted "now"
    # Backdate the stamp beyond the interval → the next hint fires again.
    (tracker / ensures.HINTED_MARKER).write_text("1", encoding="utf-8")
    ensures._reset_pending_cache()
    with caplog.at_level(logging.WARNING, logger="rebar"):
        ensures.maybe_emit_pending_hint(tracker)
    assert [r for r in caplog.records if "pending" in r.getMessage()]


# ── (b) a fresh, converged worktree never hints ───────────────────────────────
def test_converged_store_emits_nothing(fresh_repo: Path, caplog: pytest.LogCaptureFixture) -> None:
    rebar.init_repo(repo_root=str(fresh_repo))
    tracker = _tracker(fresh_repo)  # WS1 already wrote a current .ensure-applied
    with caplog.at_level(logging.WARNING, logger="rebar"):
        ensures.maybe_emit_pending_hint(tracker)
    assert [r for r in caplog.records if "pending" in r.getMessage()] == []
    assert not (tracker / ensures.HINTED_MARKER).exists()


# ── (c) hint_enabled=false suppresses even a pending store ─────────────────────
def test_hint_enabled_false_suppresses(
    fresh_repo: Path, caplog: pytest.LogCaptureFixture, monkeypatch
) -> None:
    monkeypatch.setenv("REBAR_ENSURE_HINT_ENABLED", "false")
    config.reset_config_cache()
    rebar.init_repo(repo_root=str(fresh_repo))
    tracker = _tracker(fresh_repo)
    _make_pending(tracker)
    with caplog.at_level(logging.WARNING, logger="rebar"):
        ensures.maybe_emit_pending_hint(tracker)
    assert [r for r in caplog.records if "pending" in r.getMessage()] == []
    assert not (tracker / ensures.HINTED_MARKER).exists()


# ── (d) fail-silent: a raising computation never propagates + write still commits ──
def test_hint_is_fail_silent(fresh_repo: Path, monkeypatch) -> None:
    rebar.init_repo(repo_root=str(fresh_repo))
    tracker = _tracker(fresh_repo)

    def _boom(_t):
        raise RuntimeError("boom")

    monkeypatch.setattr(ensures, "applied_ids", _boom)
    ensures._reset_pending_cache()
    # Must NOT raise.
    ensures.maybe_emit_pending_hint(tracker)


def test_write_still_commits_with_failing_hook(fresh_repo: Path, monkeypatch) -> None:
    """A real write path (write_and_push, via a comment) succeeds even when the hint
    hook raises internally — the hook is downstream of the committed write."""
    rebar.init_repo(repo_root=str(fresh_repo))
    tid = rebar.create_ticket("task", "hook test", repo_root=str(fresh_repo))

    def _boom(_t):
        raise RuntimeError("boom in hint")

    monkeypatch.setattr(ensures, "maybe_emit_pending_hint", _boom)
    # comment goes through _seam.append_event -> write_and_push -> the (raising) hook.
    rebar.comment(tid, "still commits", repo_root=str(fresh_repo))
    shown = rebar.show_ticket(tid, repo_root=str(fresh_repo))
    assert any("still commits" in c.get("body", "") for c in shown.get("comments", []))


# ── (e) read-dedup: applied_ids read at most once per process per tracker ──────
def test_applied_ids_read_once_per_process(fresh_repo: Path, monkeypatch) -> None:
    rebar.init_repo(repo_root=str(fresh_repo))
    tracker = _tracker(fresh_repo)
    _make_pending(tracker)

    calls = {"n": 0}
    real = ensures.applied_ids

    def _counting(t):
        calls["n"] += 1
        return real(t)

    monkeypatch.setattr(ensures, "applied_ids", _counting)
    ensures._reset_pending_cache()
    ensures.maybe_emit_pending_hint(tracker)
    ensures.maybe_emit_pending_hint(tracker)
    ensures.maybe_emit_pending_hint(tracker)
    assert calls["n"] == 1, "pending set must be cached — .ensure-applied read at most once"
