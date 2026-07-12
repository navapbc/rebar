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

import errno
import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import compact as _compact
from rebar._store import lock as _lock
from rebar.reducer._cache import RETIRED_SUFFIX


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


# ── I1: rename fault injection during the fold's retire/rollback loop ──────────
#
# The fold writes the SNAPSHOT atomically FIRST, then renames each folded source
# ``*.json -> *.json.retired``. On a forward-rename OSError it reverses every
# completed rename and aborts. Two failure shapes have DIFFERENT correct outcomes:
#
#   * every reverse-rename succeeds  -> a CLEAN rollback back to the pre-compaction
#     state, so the uncommitted SNAPSHOT MUST be removed (no artifact left behind);
#   * some reverse-rename ALSO fails -> the store is now mixed (a source is stuck
#     ``*.retired`` while its folded effect lives only in the SNAPSHOT), so the
#     SNAPSHOT MUST be RETAINED — removing it would silently lose that source's
#     effect (the data-loss hazard this story fixes).
#
# These are induced at the single ``os.rename`` seam the retire loop uses, scoped
# to the retirement renames (dst/src carrying ``RETIRED_SUFFIX``) so the SNAPSHOT's
# own atomic-write rename passes through untouched.


def _semantic(state: dict) -> dict:
    """A ticket's reduced state minus ``updated_at`` — the one derived field that is
    recomputed from the newest event's timestamp and so legitimately differs across a
    compaction (the SNAPSHOT is timestamped after the folded events). Everything else
    must be identical, which is what these fault tests assert."""
    return {
        k: v
        for k, v in state.items()
        # authorship_ledger is a compaction-only artifact (epic gnu-whale-ichor), not semantic state
        if k not in ("updated_at", "authorship_ledger")
    }


def _seed_foldable(repo: Path, title: str, n_events: int) -> str:
    """Create a ticket and drive ``n_events`` extra events so the fold loop has
    several sources to retire (enough to fail on the 2nd/3rd rename)."""
    tid = _seed(repo, title)
    rebar.transition(tid, "open", "in_progress", repo_root=str(repo))
    for i in range(n_events):
        rebar.comment(tid, f"c{i}", repo_root=str(repo))
    return tid


class _RenameFault:
    """A callable ``os.rename`` replacement that raises ``OSError`` on the Nth
    *retirement* rename. A forward rename is ``*.json -> *.json.retired`` (dst ends
    with ``RETIRED_SUFFIX``); a reverse (rollback) rename is
    ``*.json.retired -> *.json`` (src ends with it). Every other rename (e.g. the
    SNAPSHOT atomic write) passes straight through, so only the fold's
    retire/rollback loop is perturbed.

    Faulting is toggled with ``disarm()`` rather than ``monkeypatch.undo()``: the
    ``monkeypatch`` fixture is shared across a test, so ``undo()`` would also revert
    the autouse ``REBAR_COMPACTION_HORIZON_NS=0`` fixture and silently push the
    horizon back to the 1800 s production default (making a later re-compaction a
    no-op). Disarming leaves the (now transparent) wrapper installed."""

    def __init__(self, *, fail_forward_on: int | None = None, fail_reverse_on: int | None = None):
        self._real = os.rename
        self._fwd = 0
        self._rev = 0
        self._ff = fail_forward_on
        self._fr = fail_reverse_on

    def disarm(self) -> None:
        self._ff = None
        self._fr = None

    def __call__(self, src, dst, *a, **k):  # type: ignore[no-untyped-def]
        s, d = os.fspath(src), os.fspath(dst)
        if d.endswith(RETIRED_SUFFIX):
            self._fwd += 1
            if self._ff is not None and self._fwd == self._ff:
                raise OSError(errno.EIO, "injected forward-rename fault")
        elif s.endswith(RETIRED_SUFFIX):
            self._rev += 1
            if self._fr is not None and self._rev == self._fr:
                raise OSError(errno.EIO, "injected reverse-rename fault")
        return self._real(src, dst, *a, **k)


def test_forward_rename_fault_clean_rollback_removes_snapshot(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Forward-rename OSError with all reverses succeeding: the fold aborts, every
    retired source is reversed back to an active ``*.json``, and the uncommitted
    SNAPSHOT is removed. Reduced state is deep-equal to pre-compaction, no
    ``*.retired`` remains, and a subsequent clean compaction succeeds."""
    from rebar.reducer import reduce_ticket

    tid = _seed_foldable(rebar_repo, "fwd-fault", n_events=3)
    tdir = rebar_repo / ".tickets-tracker" / tid
    sources_before = {p.name for p in _events(rebar_repo, tid)}
    state_before = reduce_ticket(str(tdir))

    fault = _RenameFault(fail_forward_on=2)
    monkeypatch.setattr(_compact.os, "rename", fault)
    rc = _compact.compact_cli([tid, "--threshold=0", "--skip-sync"], repo_root=str(rebar_repo))
    err = capsys.readouterr().err
    assert rc == 1, err
    assert "failed to retire" in err

    # Clean rollback: no SNAPSHOT artifact, no stranded ``*.retired`` sources, the
    # original active event set is intact, and the reduced state is unchanged.
    fault.disarm()
    assert not _has_snapshot(rebar_repo, tid)
    assert not _retired(rebar_repo, tid)
    assert {p.name for p in _events(rebar_repo, tid)} == sources_before
    assert reduce_ticket(str(tdir)) == state_before  # exact: identical event set

    # fsck is CLEAN after the clean rollback — the store is back to its exact
    # pre-fold shape, so no SNAPSHOT_INCONSISTENT / ORPHAN_EVENT is reported.
    from rebar._commands import fsck as _fsck

    _fsck.fsck_cli([], repo_root=str(rebar_repo))
    fsck_out = capsys.readouterr().out
    assert "SNAPSHOT_INCONSISTENT" not in fsck_out, fsck_out
    assert "ORPHAN_EVENT" not in fsck_out, fsck_out

    # And a normal compaction still works afterwards (no residual damage).
    rc2 = _compact.compact_cli([tid, "--threshold=0", "--skip-sync"], repo_root=str(rebar_repo))
    assert rc2 == 0, capsys.readouterr().out
    assert _has_snapshot(rebar_repo, tid)
    assert _semantic(reduce_ticket(str(tdir))) == _semantic(state_before)


def test_reverse_rename_fault_retains_snapshot_reads_safe(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Forward-rename OSError followed by a reverse-rename OSError: the rollback is
    INCOMPLETE (a source is stuck ``*.retired``), so the SNAPSHOT is RETAINED, an
    explicit ``rollback incomplete; run fsck`` diagnostic is emitted, and the call
    returns failure. Crucially the mixed state is still read-correct — the retained
    SNAPSHOT's positional skip means the reversed-to-active source is not
    double-counted, so ``reduce_ticket`` matches the pre-compaction state (the
    residual inconsistency is a hygiene issue for ``fsck``, not a read error)."""
    from rebar.reducer import reduce_ticket

    tid = _seed_foldable(rebar_repo, "rev-fault", n_events=3)
    tdir = rebar_repo / ".tickets-tracker" / tid
    state_before = reduce_ticket(str(tdir))

    # Fail the 3rd forward rename (renames 1,2 succeeded), then fail the SECOND
    # reverse rename so one source reverses to active while another stays retired.
    fault = _RenameFault(fail_forward_on=3, fail_reverse_on=2)
    monkeypatch.setattr(_compact.os, "rename", fault)
    rc = _compact.compact_cli([tid, "--threshold=0", "--skip-sync"], repo_root=str(rebar_repo))
    err = capsys.readouterr().err
    fault.disarm()

    assert rc == 1, err
    assert "rollback incomplete" in err.lower(), err
    assert "fsck" in err.lower(), err

    # The SNAPSHOT is RETAINED (it carries the folded effect of the source that
    # could not be reversed) and at least one source is stranded ``*.retired``.
    assert _has_snapshot(rebar_repo, tid), "snapshot must be retained on incomplete rollback"
    assert _retired(rebar_repo, tid), "expected a source stuck as *.retired"

    # Reads are safe in the mixed window: the reduced state is unchanged.
    assert _semantic(reduce_ticket(str(tdir))) == _semantic(state_before)
