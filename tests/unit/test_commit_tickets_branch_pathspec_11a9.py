"""Pathspec-scoped locked commit seam (ticket 11a9-b11b-e93d-4832).

``reconcile_helpers._commit_binding_store_snapshot`` was the last reconciler write
composing ``git_adapter.add`` + ``commit`` outside the locked store transaction, held
there because the only seam, ``commit_and_push_tickets_branch``, stages with ``-A`` —
which sweeps in ``.bridge_state/last-pass.json`` and advances the tickets HEAD on every
idempotent pass (ticket 6454-d06e reverted that conversion). These tests pin the seam
that removes the reason for the marker: ``commit_tickets_branch`` commits ONLY a caller
pathspec, under the shared write lock, and never pushes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar._store import push

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr}")
    return completed.stdout


@pytest.fixture
def tracker(tmp_path: Path) -> Path:
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    _git(tracker, "init", "-q", "-b", "tickets")
    _git(tracker, "config", "user.name", "Test")
    _git(tracker, "config", "user.email", "t@example.com")
    (tracker / "seed.json").write_text("{}\n", encoding="utf-8")
    _git(tracker, "add", "seed.json")
    _git(tracker, "commit", "-q", "--no-verify", "-m", "seed")
    return tracker


def test_pathspec_commits_only_the_named_files(tracker: Path) -> None:
    """A pathspec commit must take exactly the named paths — a dirty sibling stays out.

    This is the 6454-d06e failure shape: ``.bridge_state/last-pass.json`` is dirty in the
    same pass, and staging it (the ``-A`` behaviour) is what broke reconcile idempotency.
    """
    bridge = tracker / ".bridge_state"
    bridge.mkdir()
    (bridge / "bindings.json").write_text('{"bindings": {}}\n', encoding="utf-8")
    (bridge / "last-pass.json").write_text('{"ts": 1}\n', encoding="utf-8")

    ok = push.commit_tickets_branch(
        tracker,
        message="persist binding-store snapshot",
        paths=[".bridge_state/bindings.json"],
        strict=True,
    )

    assert ok is True
    committed = _git(tracker, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == [".bridge_state/bindings.json"]
    # The excluded sibling is still dirty (never staged, never committed).
    assert "last-pass.json" in _git(tracker, "status", "--porcelain")


def test_unchanged_pathspec_does_not_advance_head(tracker: Path) -> None:
    """Per-file idempotency (bug 1e08): no change in any named path → HEAD unmoved,
    even while an unrelated tracker file is dirty (the ``-A`` sweep regression)."""
    bridge = tracker / ".bridge_state"
    bridge.mkdir()
    (bridge / "bindings.json").write_text('{"bindings": {}}\n', encoding="utf-8")
    assert push.commit_tickets_branch(
        tracker, message="first", paths=[".bridge_state/bindings.json"], strict=True
    )
    head = _git(tracker, "rev-parse", "HEAD").strip()

    (bridge / "last-pass.json").write_text('{"ts": 2}\n', encoding="utf-8")
    assert push.commit_tickets_branch(
        tracker, message="second", paths=[".bridge_state/bindings.json"], strict=True
    )
    assert _git(tracker, "rev-parse", "HEAD").strip() == head


def test_retirement_only_change_is_committed_via_pathspec(tracker: Path) -> None:
    """A change in ANY named path — not just the first — must commit (bug 1e08)."""
    bridge = tracker / ".bridge_state"
    bridge.mkdir()
    (bridge / "bindings.json").write_text('{"bindings": {}}\n', encoding="utf-8")
    paths = [".bridge_state/bindings.json", ".bridge_state/bindings-retired.json"]
    assert push.commit_tickets_branch(tracker, message="live", paths=paths, strict=True)

    (bridge / "bindings-retired.json").write_text('{"retired": {}}\n', encoding="utf-8")
    assert push.commit_tickets_branch(tracker, message="retire", paths=paths, strict=True)
    committed = _git(tracker, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == [".bridge_state/bindings-retired.json"]


def test_commit_tickets_branch_never_pushes(tracker: Path, monkeypatch) -> None:
    """The no-push seam preserves today's ``_commit_binding_store_snapshot`` behaviour."""

    def _fail(*_a, **_k):  # pragma: no cover - the regression this pins
        raise AssertionError("commit_tickets_branch must never push")

    monkeypatch.setattr(push, "push_tickets_branch", _fail)
    bridge = tracker / ".bridge_state"
    bridge.mkdir()
    (bridge / "bindings.json").write_text('{"bindings": {}}\n', encoding="utf-8")
    assert push.commit_tickets_branch(
        tracker, message="m", paths=[".bridge_state/bindings.json"], strict=True
    )


def test_empty_pathspec_is_a_noop(tracker: Path) -> None:
    """An explicitly-empty pathspec must never degrade into an unscoped ``-A`` sweep."""
    (tracker / "stray.json").write_text("{}\n", encoding="utf-8")
    head = _git(tracker, "rev-parse", "HEAD").strip()
    assert push.commit_tickets_branch(tracker, message="m", paths=[], strict=True)
    assert _git(tracker, "rev-parse", "HEAD").strip() == head
    assert "stray.json" in _git(tracker, "status", "--porcelain")
