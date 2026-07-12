"""Real compactor process-death (SIGKILL) recovery (story 44b7).

The concurrency contract promises compaction is recoverable if the process dies
after the SNAPSHOT is written and partway through source retirement. Existing tests
only monkeypatch Python exceptions (which still run ``except``/cleanup code) or build
orphan states by hand — none kill a real compactor. ``SIGKILL`` runs NONE of the
rollback, lock-release, cache-cleanup, or git-staging code, so this is the only test
that exercises the true process-death durability contract (stale write-lock
reclamation + a dirty tracker worktree left between the SNAPSHOT write and the commit).

Determinism comes from a test-only failpoint in ``compact.py``
(``REBAR_TEST_COMPACT_RENAME_BARRIER``) that pauses the child right after the FIRST
source rename and signals readiness via a marker file — no reliance on a timing race.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import rebar
from rebar.reducer import reduce_ticket

pytestmark = pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"), reason="SIGKILL-based process-death test is POSIX-only"
)


def _events(repo: Path, tid: str) -> list[Path]:
    tdir = repo / ".tickets-tracker" / tid
    return [p for p in tdir.glob("*.json") if not p.name.startswith(".")]


def _active_sources(repo: Path, tid: str) -> list[Path]:
    return [p for p in _events(repo, tid) if not p.name.endswith("-SNAPSHOT.json")]


def _snapshots(repo: Path, tid: str) -> list[Path]:
    return [p for p in _events(repo, tid) if p.name.endswith("-SNAPSHOT.json")]


def _retired(repo: Path, tid: str) -> list[Path]:
    return list((repo / ".tickets-tracker" / tid).glob("*.retired"))


def _all_source_uuids(repo: Path, tid: str) -> set[str]:
    """Every event UUID present on disk across active ``*.json`` and ``*.retired``
    (a UUID is the middle field of ``{ts}-{uuid}-{TYPE}.json``)."""
    tdir = repo / ".tickets-tracker" / tid
    uuids: set[str] = set()
    for p in list(tdir.glob("*.json")) + list(tdir.glob("*.retired")):
        if p.name.startswith(".") or "-SNAPSHOT." in p.name:
            continue
        stem = p.name.split(".json")[0]
        parts = stem.split("-", 1)
        if len(parts) == 2:
            uuids.add(parts[1].rsplit("-", 1)[0])
    return uuids


def _semantic(state: dict) -> dict:
    return {
        k: v
        for k, v in state.items()
        # authorship_ledger is a compaction-only artifact (epic gnu-whale-ichor), not semantic state
        if k not in ("updated_at", "authorship_ledger")
    }


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


def test_sigkill_mid_retirement_recovers_from_fresh_process(
    rebar_repo: Path, tmp_path: Path
) -> None:
    tid = _seed_foldable(rebar_repo, "sigkill", n_events=3)
    tdir = rebar_repo / ".tickets-tracker" / tid
    state_before = reduce_ticket(str(tdir))
    uuids_before = _all_source_uuids(rebar_repo, tid)

    barrier = tmp_path / "barrier"
    barrier.mkdir()
    env = dict(os.environ)
    env["REBAR_TEST_COMPACT_RENAME_BARRIER"] = str(barrier)
    env["REBAR_COMPACTION_HORIZON_NS"] = "0"

    proc = subprocess.Popen(
        [sys.executable, "-m", "rebar.cli", "compact", tid, "--threshold=0", "--skip-sync"],
        cwd=str(rebar_repo),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Wait (deterministically) for the child to reach the mid-retirement barrier.
        reached = barrier / "reached"
        deadline = time.monotonic() + 30.0
        while not reached.exists():
            if proc.poll() is not None:  # child exited/crashed before the barrier
                raise AssertionError(f"compactor exited early rc={proc.returncode}")
            if time.monotonic() > deadline:
                raise AssertionError("compactor never reached the rename barrier")
            time.sleep(0.02)

        # Mid-retirement window: SNAPSHOT written, EXACTLY ONE source retired, the rest
        # still active, nothing committed yet.
        assert len(_snapshots(rebar_repo, tid)) == 1, "SNAPSHOT must be written before renames"
        assert len(_retired(rebar_repo, tid)) == 1, "expected exactly one source retired"
        assert _active_sources(rebar_repo, tid), "the remaining sources should still be active"

        # SIGKILL: none of the rollback/lock-release/commit code runs.
        os.kill(proc.pid, signal.SIGKILL)
    finally:
        proc.wait(timeout=30)

    # ── Recovery from a FRESH process ─────────────────────────────────────────
    # 1. Reads are correct at the exact pre-crash semantic state (the retained
    #    SNAPSHOT's positional skip covers the half-retired sources).
    assert _semantic(reduce_ticket(str(tdir))) == _semantic(state_before)

    # 2. No source UUID was lost across the crash (all live in active or *.retired).
    assert uuids_before.issubset(_all_source_uuids(rebar_repo, tid))

    # 3. fsck reports the documented, repairable inconsistency (a SNAPSHOT plus
    #    still-active folded sources) — not a clean bill, not an unrepairable error.
    rc_fsck, fsck_out = _fsck_subprocess(rebar_repo)
    assert rc_fsck != 0, fsck_out
    assert "SNAPSHOT_INCONSISTENT" in fsck_out, fsck_out

    # 4. The write lock is reclaimable by a fresh process (the SIGKILL'd holder left a
    #    stale lock) AND the documented repair converges: run it via a brand-new CLI
    #    subprocess so we prove real cross-process lock reclamation, not in-process reuse.
    rc_repair, repair_out = _fsck_subprocess(rebar_repo, "--repair-snapshots")
    assert rc_repair == 0, repair_out

    # 5. After repair fsck is clean and the reduced state is unchanged.
    rc_clean, clean_out = _fsck_subprocess(rebar_repo)
    assert rc_clean == 0, clean_out
    assert "SNAPSHOT_INCONSISTENT" not in clean_out
    assert _semantic(reduce_ticket(str(tdir))) == _semantic(state_before)


def _fsck_subprocess(repo: Path, *flags: str) -> tuple[int, str]:
    """Run ``rebar fsck [flags]`` in a brand-new CLI process so we exercise real
    cross-process behavior (including write-lock reclamation for ``--repair-*``)."""
    out = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "fsck", *flags],
        cwd=str(repo),
        env={**os.environ, "REBAR_COMPACTION_HORIZON_NS": "0"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    return out.returncode, out.stdout + out.stderr
