"""Held-out recovery oracle for the compaction rollback-atomicity fix (story 4001).

``test_compact_negative.py`` pins the in-process *behavior* of the fold's
retire/rollback loop (clean rollback removes the SNAPSHOT; an incomplete rollback
RETAINS it and warns). This module verifies the END-TO-END store-recovery contract
that retention enables — that the residual mixed state produced by an incomplete
rollback is exactly what ``fsck`` already detects and ``fsck --repair-snapshots``
already repairs, with no ticket state lost across the round-trip.

These assertions target the observable ``fsck`` surface and the reduced ticket
state, never compaction internals, so they hold under any behavior-preserving
refactor of the rollback branch.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

import rebar
from rebar._commands import compact as _compact
from rebar._commands import fsck as _fsck
from rebar.reducer import reduce_ticket
from rebar.reducer._cache import RETIRED_SUFFIX


def _seed_foldable(repo: Path, title: str, n_events: int) -> str:
    tid = rebar.create_ticket(
        "task",
        title,
        description="Body.\n\n## Acceptance Criteria\n- [ ] a",
        repo_root=str(repo),
    )
    rebar.transition(tid, "open", "in_progress", repo_root=str(repo))
    for i in range(n_events):
        rebar.comment(tid, f"c{i}", repo_root=str(repo))
    return tid


def _events(repo: Path, tid: str) -> list[Path]:
    tdir = repo / ".tickets-tracker" / tid
    return [p for p in tdir.glob("*.json") if not p.name.startswith(".")]


def _has_snapshot(repo: Path, tid: str) -> bool:
    return any(p.name.endswith("-SNAPSHOT.json") for p in _events(repo, tid))


def _retired(repo: Path, tid: str) -> list[Path]:
    return list((repo / ".tickets-tracker" / tid).glob("*.retired"))


def _semantic(state: dict) -> dict:
    """Reduced state minus the derived ``updated_at`` (recomputed from the newest
    event's timestamp, so it legitimately shifts when a SNAPSHOT is rebuilt)."""
    return {
        k: v
        for k, v in state.items()
        # authorship_ledger is a compaction-only artifact (epic gnu-whale-ichor), not semantic state
        if k not in ("updated_at", "authorship_ledger")
    }


class _RenameFault:
    """``os.rename`` replacement that faults on the Nth retirement rename; disarmed
    (not ``monkeypatch.undo()``-ed) so the shared autouse ``REBAR_COMPACTION_HORIZON_NS=0``
    fixture is left intact for any subsequent compaction."""

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


def _make_mixed_state(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[str, Path, dict]:
    """Drive an incomplete rollback so the ticket lands in the retained-snapshot
    mixed state, and return ``(tid, tdir, state_before)``."""
    tid = _seed_foldable(repo, "mixed", n_events=3)
    tdir = repo / ".tickets-tracker" / tid
    state_before = reduce_ticket(str(tdir))
    fault = _RenameFault(fail_forward_on=3, fail_reverse_on=2)
    monkeypatch.setattr(_compact.os, "rename", fault)
    rc = _compact.compact_cli([tid, "--threshold=0", "--skip-sync"], repo_root=str(repo))
    capsys.readouterr()
    fault.disarm()
    assert rc == 1
    assert _has_snapshot(repo, tid) and _retired(repo, tid)
    return tid, tdir, state_before


def test_fsck_flags_reverse_rename_mixed_state_and_not_healthy(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Plain ``fsck`` reports SNAPSHOT_INCONSISTENT for the reversed-to-active source
    left by an incomplete rollback (its UUID is in the retained snapshot yet a live
    ``*.json`` still exists), and does NOT flag a healthy, fully-compacted ticket."""
    bad_tid, _, _ = _make_mixed_state(rebar_repo, monkeypatch, capsys)

    # A separately-created ticket that compacts cleanly must stay healthy.
    good_tid = _seed_foldable(rebar_repo, "healthy", n_events=2)
    rc = _compact.compact_cli([good_tid, "--threshold=0", "--skip-sync"], repo_root=str(rebar_repo))
    assert rc == 0, capsys.readouterr().out

    _fsck.fsck_cli([], repo_root=str(rebar_repo))
    out = capsys.readouterr().out
    assert "SNAPSHOT_INCONSISTENT" in out, out
    assert bad_tid in out, out
    # The healthy ticket must not be implicated.
    for line in out.splitlines():
        if "SNAPSHOT_INCONSISTENT" in line:
            assert good_tid not in line, line


def test_fsck_repair_snapshots_rebuilds_mixed_state(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``fsck --repair-snapshots`` reconciles the mixed state by REBUILDING: a fresh
    ``*-SNAPSHOT.json`` is written, the formerly-active source is renamed to
    ``*.retired``, a follow-up ``fsck`` exits 0, and the reduced state is unchanged
    (deep-equal to the pre-repair reading — no UUID/content lost)."""
    tid, tdir, _ = _make_mixed_state(rebar_repo, monkeypatch, capsys)

    def _snapshot_names() -> set[str]:
        return {p.name for p in _events(rebar_repo, tid) if p.name.endswith("-SNAPSHOT.json")}

    state_pre_repair = reduce_ticket(str(tdir))
    active_json_pre = {p.name for p in _events(rebar_repo, tid) if "-SNAPSHOT.json" not in p.name}
    snap_pre = _snapshot_names()
    assert active_json_pre, "expected at least one reversed-to-active source before repair"

    rc = _fsck.fsck_cli(["--repair-snapshots"], repo_root=str(rebar_repo))
    out = capsys.readouterr().out
    assert rc == 0, out

    # A NEW snapshot was written (the id changed) and the previously-active source
    # is now retired — no active non-snapshot event remains.
    snap_post = _snapshot_names()
    assert snap_post and snap_post != snap_pre, f"want rebuilt snapshot; {snap_pre}->{snap_post}"
    active_json_post = {p.name for p in _events(rebar_repo, tid) if "-SNAPSHOT.json" not in p.name}
    assert not active_json_post, f"sources should be retired after repair: {active_json_post}"
    retired_bases = {p.name[: -len(RETIRED_SUFFIX)] for p in _retired(rebar_repo, tid)}
    assert active_json_pre.issubset(retired_bases)

    # A follow-up fsck is clean and the reduced state is untouched by the repair.
    rc2 = _fsck.fsck_cli([], repo_root=str(rebar_repo))
    clean = capsys.readouterr().out
    assert rc2 == 0, clean
    assert "SNAPSHOT_INCONSISTENT" not in clean, clean
    assert _semantic(reduce_ticket(str(tdir))) == _semantic(state_pre_repair)
