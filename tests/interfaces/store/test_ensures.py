"""WS1: the ensure-registry (``rebar._store.ensures``) + init wiring.

Pins the registry shape (a frozen id set — a rename/typo can't silently re-pend a
store), that ``run_ensures`` converges + writes the ``.ensure-applied`` marker under
the store write lock, that it NEVER aborts its caller (a raising unit or an
unavailable write lock is swallowed), and that both init entry points (``init_core``
and ``_init_via_symlink``) route through the registry. The exhaustive behavioural
suite (idempotency git-log, hot-path, concurrency, no_mutate, absent-marker) lives
in WS5 (``test_ensure_invariants.py``); this file owns the WS1 wiring contract.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._store import ensures
from rebar._store.lock import LockTimeout


@pytest.fixture
def fresh_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repo WITHOUT a rebar tracker (no init yet)."""
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


def _tracker(repo: Path) -> Path:
    return repo / ".tickets-tracker"


def _tickets_head(tracker: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(tracker), "rev-parse", "tickets"],
        capture_output=True,
        text=True,
    ).stdout.strip()


# ── registry shape ───────────────────────────────────────────────────────────
def test_registry_id_set_is_frozen() -> None:
    """The advertised id list and the actual callable map must agree — a rename or
    a typo (which would silently strand a unit as forever-pending) is caught here."""
    expected = {"env-id", "gc-config", "merge-ours", "gitattributes", "gitignore", "store-compat"}
    assert set(ensures.REGISTRY_IDS) == expected
    assert set(ensures._registry().keys()) == expected
    assert ensures.registry_ids() == frozenset(expected)


# ── run_ensures converges + writes the marker ────────────────────────────────
def test_run_ensures_converges_and_writes_marker(fresh_repo: Path) -> None:
    rebar.init_repo(repo_root=str(fresh_repo))
    tracker = _tracker(fresh_repo)

    # Fresh init already ran the sweep: the marker names every non-failed unit.
    applied = ensures.applied_ids(tracker)
    assert applied == set(ensures.REGISTRY_IDS)

    # Every unit reports a non-failed outcome on a converged store.
    outcomes = ensures.run_ensures(tracker)
    assert {o.id for o in outcomes} == set(ensures.REGISTRY_IDS)
    assert all(o.status in ("ok", "changed") for o in outcomes)


def test_run_ensures_idempotent_no_new_commits(fresh_repo: Path) -> None:
    """WS1 AC3 / WS5 AC1: a second sweep on a converged store makes zero commits."""
    rebar.init_repo(repo_root=str(fresh_repo))
    tracker = _tracker(fresh_repo)
    before = _tickets_head(tracker)

    outcomes = ensures.run_ensures(tracker)

    assert _tickets_head(tracker) == before, "converged sweep must not create commits"
    assert all(o.status == "ok" for o in outcomes), outcomes


# ── the marker is git-ignored + robust to absent/garbage ─────────────────────
def test_marker_is_gitignored(fresh_repo: Path) -> None:
    rebar.init_repo(repo_root=str(fresh_repo))
    tracker = _tracker(fresh_repo)
    gitignore = subprocess.run(
        ["git", "-C", str(tracker), "show", "tickets:.gitignore"],
        capture_output=True,
        text=True,
    ).stdout
    assert ensures.APPLIED_MARKER in gitignore.splitlines()
    # The written marker is ignored — it never shows up as an untracked change.
    assert (tracker / ensures.APPLIED_MARKER).is_file()
    untracked = subprocess.run(
        ["git", "-C", str(tracker), "status", "--porcelain"],
        capture_output=True,
        text=True,
    ).stdout
    assert ensures.APPLIED_MARKER not in untracked


def test_applied_ids_absent_and_garbage_degrade_to_empty(fresh_repo: Path) -> None:
    rebar.init_repo(repo_root=str(fresh_repo))
    tracker = _tracker(fresh_repo)
    marker = tracker / ensures.APPLIED_MARKER

    marker.unlink(missing_ok=True)
    assert ensures.applied_ids(tracker) == set(), "absent marker → empty set"

    marker.write_text("}{not json", encoding="utf-8")
    assert ensures.applied_ids(tracker) == set(), "garbage marker → empty set"

    marker.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    assert ensures.applied_ids(tracker) == set(), "non-list JSON → empty set"


# ── never aborts the caller ──────────────────────────────────────────────────
def test_run_ensures_swallows_lock_timeout(fresh_repo: Path, monkeypatch) -> None:
    """A write-lock acquisition failure is treated like a whole-sweep no-op —
    run_ensures returns without raising and writes no marker."""
    rebar.init_repo(repo_root=str(fresh_repo))
    tracker = _tracker(fresh_repo)

    def _boom(*_a, **_k):
        raise LockTimeout(60)

    monkeypatch.setattr(ensures, "write_lock", _boom)
    outcomes = ensures.run_ensures(tracker)  # must NOT raise
    assert outcomes == []


def test_run_ensures_skips_and_continues_on_unit_raise(fresh_repo: Path, monkeypatch) -> None:
    """A raising unit is caught (→ failed) and excluded from the marker; the other
    units still run and are recorded."""
    rebar.init_repo(repo_root=str(fresh_repo))
    tracker = _tracker(fresh_repo)

    real = ensures._registry()

    def _explode(_tracker: str):
        raise RuntimeError("boom")

    def _patched_registry() -> dict:
        return {**real, "gc-config": _explode}

    monkeypatch.setattr(ensures, "_registry", _patched_registry)
    outcomes = ensures.run_ensures(tracker)

    by_id = {o.id: o for o in outcomes}
    assert by_id["gc-config"].status == "failed"
    assert all(
        by_id[u].status in ("ok", "changed") for u in ensures.REGISTRY_IDS if u != "gc-config"
    )
    # The failed unit is excluded from the marker; the rest are present.
    assert "gc-config" not in ensures.applied_ids(tracker)
    assert set(ensures.REGISTRY_IDS) - {"gc-config"} <= ensures.applied_ids(tracker)


def test_init_continues_when_write_lock_unavailable(fresh_repo: Path, monkeypatch) -> None:
    """init must return 0 (never abort) even if the ensure sweep cannot take the
    write lock — the store is merely left to converge on a later init/fsck."""
    from rebar._commands import init as init_mod

    def _boom(*_a, **_k):
        raise LockTimeout(60)

    monkeypatch.setattr(ensures, "write_lock", _boom)
    assert init_mod.init_core(repo_root=str(fresh_repo)) == 0


# ── both init entry points route through the registry ────────────────────────
def test_symlink_worktree_converges_via_registry(
    fresh_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A linked worktree attaches by symlink to the main store and still runs the
    ensure sweep (its `.ensure-applied` reflects the shared, converged store)."""
    rebar.init_repo(repo_root=str(fresh_repo))
    assert ensures.applied_ids(_tracker(fresh_repo)) == set(ensures.REGISTRY_IDS)

    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(fresh_repo), "worktree", "add", "-q", str(wt)], check=True)
    monkeypatch.setenv("REBAR_ROOT", str(wt))
    # Attaching the worktree symlinks .tickets-tracker to the main store and sweeps.
    rebar.init_repo(repo_root=str(wt))
    assert (wt / ".tickets-tracker").is_symlink()
    assert ensures.applied_ids(wt / ".tickets-tracker") == set(ensures.REGISTRY_IDS)
