"""The review-bot boot path bounds the ensure-sweep write-lock budget (bug e43f).

``run_ensures`` acquires the store write lock with ``write_lock``'s default budget of
``_DEFAULT_TIMEOUT`` (30s) × ``_DEFAULT_ATTEMPTS`` (2) = 60s. On the review-bot boot path
(``opcert_service.workspace._populate``) that runs behind the autodeploy health check
(``HEALTH_TIMEOUT=30``), so any genuinely contended lock — a concurrent writer, a slow
push, a correctly-never-reclaimed foreign-host lock — can stall the sweep for a full minute
and fail the deploy on its own, with no orphaned lock involved. 304e/castoff-tigerseye-
ammonite fixed the reclaimability defect but left this budget untouched.

The fix gives the boot sweep a SHORT bounded budget (``workspace._ENSURE_BOOT_TIMEOUT`` ×
``_ENSURE_BOOT_ATTEMPTS``, mirroring the MCP-boot budget), so a contended lock SKIPS the sweep
(idempotent — it re-runs next boot) rather than delaying boot past the health check.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import rebar
from rebar._store import ensures
from rebar._store import lock as _lock
from rebar._store.compat import StoreIncompatibleError
from rebar.opcert_service import workspace
from rebar.opcert_service.config import OpcertServiceConfig

pytestmark = pytest.mark.unit

_AC = (
    "## Acceptance Criteria\n"
    "- [ ] the widget is built and wired to the CLI\n"
    "- [ ] tests cover the happy path and one edge case\n\n"
    "See src/rebar/widget.py for the implementation surface. This description is long enough to "
    "clear the clarity floor and carries a checklist so the gates would pass."
)


def _run(cwd: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _make_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A local rebar store serving as BOTH the review (code) and tickets (state) remote."""
    src = tmp_path / "authoritative"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True, capture_output=True)
    _run(str(src), "config", "user.email", "src@e.test")
    _run(str(src), "config", "user.name", "src")
    _run(str(src), "commit", "-q", "--allow-empty", "-m", "genesis")
    monkeypatch.setenv("REBAR_ROOT", str(src))
    rebar.init_repo(repo_root=str(src))
    rebar.create_ticket("story", "build the widget", description=_AC, repo_root=str(src))
    monkeypatch.delenv("REBAR_ROOT", raising=False)
    return str(src)


def _cfg(source_url: str) -> OpcertServiceConfig:
    return OpcertServiceConfig(
        review_remote_url=source_url,
        tickets_remote_url=source_url,
        review_branch="main",
        guard="secret",
        env_id="nava-opcert-test-1",
        key_path="/run/secrets/opcert-ed25519-key",
        job_timeout_seconds=900.0,
        port=8080,
    )


@pytest.fixture
def fresh_tracker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A freshly-initialised rebar store; returns its tracker dir."""
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
    rebar.init_repo(repo_root=str(repo))
    return repo / ".tickets-tracker"


def test_boot_budget_is_short_and_well_under_the_default() -> None:
    """The declared boot budget must be a few seconds at most — far below write_lock's
    30×2=60s default and below the review-bot's 30s deploy health check."""
    budget = workspace._ENSURE_BOOT_TIMEOUT * workspace._ENSURE_BOOT_ATTEMPTS
    default = _lock._DEFAULT_TIMEOUT * _lock._DEFAULT_ATTEMPTS
    assert budget <= 5
    assert budget < default
    assert budget < 30  # the autodeploy HEALTH_TIMEOUT


def test_reviewbot_boot_passes_a_bounded_budget_to_run_ensures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION GUARD: the real review-bot boot path (prepare_workspace → _populate) must
    call run_ensures with a bounded budget, never the 60s default. Spy on run_ensures at the
    module it is imported from and assert the recorded timeout/attempts."""
    src = _make_source(tmp_path, monkeypatch)
    cfg = _cfg(src)

    recorded: list[tuple[int | None, int | None]] = []

    def _spy(_tracker, *, timeout=None, attempts=None):
        recorded.append((timeout, attempts))
        return []

    monkeypatch.setattr(ensures, "run_ensures", _spy)

    ws = workspace.prepare_workspace(cfg)
    workspace.discard(ws.repo_root)

    assert recorded, "the boot path did not run the ensure sweep at all"
    timeout, attempts = recorded[0]
    assert timeout is not None and attempts is not None, "boot path used write_lock's defaults"
    assert timeout * attempts <= 5, f"boot budget {timeout}×{attempts} is not bounded"


def test_boot_budget_skips_promptly_when_the_lock_is_held(
    fresh_tracker: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """THE FIX, end to end: with the boot budget, a run_ensures against a HELD write lock
    returns promptly (well under the 60s default) as a no-op, and the skip is logged — it
    does not stall boot. This is what keeps a contended lock from blowing the health check."""
    held = _lock.acquire(str(fresh_tracker))  # occupy the write lock
    try:
        with caplog.at_level(logging.WARNING, logger="rebar"):
            start = time.monotonic()
            outcomes = ensures.run_ensures(
                fresh_tracker,
                timeout=workspace._ENSURE_BOOT_TIMEOUT,
                attempts=workspace._ENSURE_BOOT_ATTEMPTS,
            )
            elapsed = time.monotonic() - start
    finally:
        held.release()

    assert outcomes == [], "a contended sweep must skip, not partially run"
    # timing: hang-guard — lock-wait guard; a contended sweep must skip instantly
    assert elapsed < 30, f"boot sweep waited {elapsed:.1f}s — nowhere near a few seconds"
    assert any("skipping sweep" in r.getMessage() for r in caplog.records), "skip not logged"


def test_skipped_sweep_reruns_once_the_lock_is_free(fresh_tracker: Path) -> None:
    """A skipped sweep is not lost: once the lock is free the sweep converges on the next
    boot — the budget only defers, never drops, the work."""
    held = _lock.acquire(str(fresh_tracker))
    try:
        assert (
            ensures.run_ensures(
                fresh_tracker,
                timeout=workspace._ENSURE_BOOT_TIMEOUT,
                attempts=workspace._ENSURE_BOOT_ATTEMPTS,
            )
            == []
        )
    finally:
        held.release()

    reran = ensures.run_ensures(fresh_tracker)  # lock now free
    assert reran, "the sweep did not re-run once the lock was released"
    assert set(ensures.REGISTRY_IDS) <= ensures.applied_ids(fresh_tracker)


def test_store_incompatible_still_fails_closed_under_the_boot_budget(
    fresh_tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: the fail-closed StoreIncompatibleError re-raise is unaffected by the short budget —
    an incompatible store must still refuse, never be swallowed into a benign sweep no-op."""

    def _incompatible(*_a, **_k):
        raise StoreIncompatibleError("store is from a newer rebar")

    # ensures late-binds through its `_lock` module reference (bug d720-fc72): stub that
    # seam rather than the shared lock module.
    monkeypatch.setattr(
        ensures,
        "_lock",
        SimpleNamespace(
            canonical_tracker=_lock.canonical_tracker,
            write_lock=_incompatible,
            LockTimeout=_lock.LockTimeout,
        ),
    )
    with pytest.raises(StoreIncompatibleError):
        ensures.run_ensures(
            fresh_tracker,
            timeout=workspace._ENSURE_BOOT_TIMEOUT,
            attempts=workspace._ENSURE_BOOT_ATTEMPTS,
        )
