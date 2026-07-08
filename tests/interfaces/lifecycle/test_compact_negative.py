"""Compact negative / branch coverage: ``compact-all --limit`` and the
``compact`` error paths (lock-timeout, git-failure).

The happy-path compaction is covered in test_signature.py; this pins the paths a
review flagged as untested:

  * ``compact-all --limit N`` compacts only the first N tickets needing a SNAPSHOT
    (the others are left for a later run);
  * ``compact`` surfaces a lock-timeout as a non-zero exit (the lock seam raises
    ``LockTimeout``) without writing a SNAPSHOT or deleting events;
  * ``compact`` surfaces a git-failure (the staged-commit ``git`` call fails) as a
    non-zero exit, again without corrupting the store.

Both error paths are induced at the cleanest module seam: ``compact.lock.acquire``
(the single lock entry the critical section uses) and ``compact._git`` (the single
git shim every commit goes through). After each, the store still reduces and the
ticket keeps its original status.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import compact as _compact
from rebar._store import lock as _lock


def _seed(repo: Path, title: str) -> str:
    return rebar.create_ticket(
        "task",
        title,
        description="Body.\n\n## Acceptance Criteria\n- [ ] a",
        repo_root=str(repo),
    )


def _events(repo: Path, tid: str) -> list[Path]:
    tdir = repo / ".tickets-tracker" / tid
    return [p for p in tdir.glob("*.json") if not p.name.startswith(".")]


def _has_snapshot(repo: Path, tid: str) -> bool:
    return any(p.name.endswith("-SNAPSHOT.json") for p in _events(repo, tid))


def _retired(repo: Path, tid: str) -> list[Path]:
    tdir = repo / ".tickets-tracker" / tid
    return list(tdir.glob("*.retired"))


# ── I1: compaction retires (renames) folded events instead of deleting them ────
def test_compact_retires_folded_events_not_deleted(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """b306 (invariant I1): compaction must RENAME each folded source event to
    ``*.retired`` — never ``os.remove`` it. A hard delete can be resurrected by a
    delete/add reconciliation (the RC1 rebase class) and then trips
    SNAPSHOT_INCONSISTENT; an append-only rename preserves the bytes and is
    invisible to replay/fsck. RED on the pre-patch code (which deleted the sources,
    so no ``*.retired`` files exist)."""
    from rebar._commands import fsck as _fsck
    from rebar.reducer import reduce_ticket

    tid = _seed(rebar_repo, "retire-me")
    rebar.transition(tid, "open", "in_progress", repo_root=str(rebar_repo))
    rebar.comment(tid, "one", repo_root=str(rebar_repo))
    rebar.comment(tid, "two", repo_root=str(rebar_repo))

    tdir = rebar_repo / ".tickets-tracker" / tid
    sources_before = {p.name for p in _events(rebar_repo, tid)}
    state_before = reduce_ticket(str(tdir))

    rc = _compact.compact_cli([tid, "--threshold=0", "--skip-sync"], repo_root=str(rebar_repo))
    assert rc == 0, capsys.readouterr().out

    # A SNAPSHOT is the only remaining ACTIVE event; the folded sources survive as
    # ``*.retired`` (not deleted) — one retired file per folded source.
    assert _has_snapshot(rebar_repo, tid)
    active = {p.name for p in _events(rebar_repo, tid)}
    assert all(n.endswith("-SNAPSHOT.json") for n in active), active
    retired = {p.name for p in _retired(rebar_repo, tid)}
    assert retired, "folded sources were deleted, not retired to *.retired"
    assert retired == {n + ".retired" for n in sources_before}

    # Replay ignores ``*.retired`` and reproduces the pre-compaction status.
    state_after = reduce_ticket(str(tdir))
    assert state_after["status"] == state_before["status"] == "in_progress"

    # fsck sees the retired sources but does NOT flag SNAPSHOT_INCONSISTENT/ORPHAN.
    _fsck.fsck_cli([], repo_root=str(rebar_repo))
    fsck_out = capsys.readouterr().out
    assert "SNAPSHOT_INCONSISTENT" not in fsck_out, fsck_out
    assert "ORPHAN_EVENT" not in fsck_out, fsck_out

    # Idempotent: a re-compact at the default threshold is below-threshold (only the
    # lone SNAPSHOT remains active) — a no-op that retires nothing further.
    rc2 = _compact.compact_cli([tid, "--skip-sync"], repo_root=str(rebar_repo))
    assert rc2 == 0
    assert {p.name for p in _retired(rebar_repo, tid)} == retired


# ── compact-all --limit ───────────────────────────────────────────────────────
def test_compact_all_limit_compacts_only_n(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tids = [_seed(rebar_repo, f"t{i}") for i in range(4)]
    # None have a SNAPSHOT yet (created tickets carry only a CREATE event).
    assert not any(_has_snapshot(rebar_repo, t) for t in tids)

    rc = _compact.compact_all_cli(["--limit=2", "--no-commit"], repo_root=str(rebar_repo))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "Applying --limit=2" in out
    assert "2 compacted" in out

    compacted = [t for t in tids if _has_snapshot(rebar_repo, t)]
    assert len(compacted) == 2, f"expected exactly 2 compacted, got {compacted}"


# ── compact lock-timeout ──────────────────────────────────────────────────────
def test_compact_lock_timeout_is_surfaced_without_corruption(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _seed(rebar_repo, "locked")
    before = {p.name for p in _events(rebar_repo, tid)}

    def _boom(*_a: object, **_k: object) -> object:
        raise _lock.LockTimeout(30)

    monkeypatch.setattr(_compact.lock, "acquire", _boom)
    rc = _compact.compact_cli([tid, "--threshold=0", "--skip-sync"], repo_root=str(rebar_repo))
    err = capsys.readouterr().err
    assert rc == 1
    assert "could not acquire lock" in err

    # No SNAPSHOT written, no events deleted; the store still reduces cleanly.
    assert not _has_snapshot(rebar_repo, tid)
    assert {p.name for p in _events(rebar_repo, tid)} == before
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "open"


# ── compact git-failure ───────────────────────────────────────────────────────
def test_compact_git_failure_is_surfaced(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = _seed(rebar_repo, "gitfail")
    real_git = _compact._git

    def _fail_on_add(tracker: str, *args: str) -> subprocess.CompletedProcess:
        # Fail the staged-add inside the locked critical section; let gc.auto / diff
        # config calls through so we exercise the in-lock commit error branch.
        if args[:1] == ("add",):
            return subprocess.CompletedProcess(args, 1, "", "git add boom")
        return real_git(tracker, *args)

    monkeypatch.setattr(_compact, "_git", _fail_on_add)
    rc = _compact.compact_cli([tid, "--threshold=0", "--skip-sync"], repo_root=str(rebar_repo))
    err = capsys.readouterr().err
    assert rc == 1
    assert "git operation failed" in err

    # The ticket still reduces and keeps its status (no corruption from the abort).
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "open"
