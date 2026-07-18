"""Batched-import benchmark + crash-resume (epic cold-stall-chalk / B4).

Proves the write-time batching actually reduces commits and is crash-safe:

- **Benchmark** — a synthetic N=3000 CREATE+comment import makes exactly
  ``2*ceil(3000/256)`` commits (Pass 1 + Pass 2d), a >=10x reduction vs the ~2N
  per-event baseline.
- **Malformed-event-mid-chunk** — a bad event in a batch rolls the whole chunk back
  (index clean, commit count unchanged) — exercised at the primitive level.
- **Crash-resume** — a crash before a LATER chunk's commit leaves whole-commit-or-
  none; the re-run's ``source_id`` re-scan skips the committed chunks and re-emits
  the rest, ending with every ticket exactly once.

Records are synthesized as dicts (the importer accepts an iterable of dicts), so no
slow interactive seeding of a source store is needed.
"""

from __future__ import annotations

import math
import subprocess
import time
from pathlib import Path

import pytest

import rebar
from rebar import config
from rebar._store import event_append


def _fresh_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    rebar.init_repo(repo_root=str(repo))
    return repo


def _tracker(repo: Path) -> str:
    return str(config.tracker_dir(str(repo)))


def _commit_count(repo: Path) -> int:
    # `git rev-list --count HEAD` can transiently fail under CI load (rc!=0, empty stdout) —
    # bug efb7-09de: the old `int(r.stdout.strip())` turned that into an opaque
    # `ValueError: invalid literal for int()` that masked git's real error and flaked the test.
    # Retry the transient; on a persistent failure raise a clear diagnostic carrying git's stderr.
    tracker = _tracker(repo)
    last = None
    for attempt in range(5):
        r = subprocess.run(
            ["git", "-C", tracker, "rev-list", "--count", "HEAD"],
            capture_output=True,
            text=True,
        )
        out = r.stdout.strip()
        if r.returncode == 0 and out:
            return int(out)
        last = r
        if attempt < 4:
            time.sleep(0.05)
    raise RuntimeError(
        f"git rev-list --count HEAD failed after retries in {tracker}: "
        f"rc={last.returncode} stdout={last.stdout.strip()!r} stderr={last.stderr.strip()!r}"
    )


def _records(n: int, *, with_comment: bool) -> list[dict]:
    """n synthetic ticket records — CREATE only, optionally one comment each. No
    parents/links/statuses, so only Pass 1 (+ Pass 2d) carry events."""
    recs = []
    for i in range(n):
        rec = {"ticket_id": f"src-{i:05d}", "ticket_type": "task", "title": f"T{i}"}
        if with_comment:
            rec["comments"] = [{"body": f"c{i}"}]
        recs.append(rec)
    return recs


@pytest.mark.integration
def test_benchmark_commit_count_is_ceil_per_pass(tmp_path: Path) -> None:
    dst = _fresh_repo(tmp_path, "bench")
    n = 3000
    before = _commit_count(dst)
    meta = rebar.import_tickets(_records(n, with_comment=True), repo_root=str(dst))
    delta = _commit_count(dst) - before

    assert meta["created"] == n
    assert meta["comments"] == n
    # Pass 1 (n CREATEs) + Pass 2d (n comments), each flushed in 256-event chunks.
    expected = 2 * math.ceil(n / 256)
    assert delta == expected, f"expected {expected} batched commits, got {delta}"
    # A >=10x reduction vs the ~2n per-event baseline.
    assert delta * 10 <= 2 * n


def test_commit_count_tolerates_transient_git_failure_not_opaque_int_crash(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression (bug efb7-09de): _commit_count must NOT crash with the opaque
    `ValueError: invalid literal for int() with base 10: ''` when `git rev-list --count HEAD`
    transiently returns empty stdout (the observed CI flake, run 29620766631). It must:
      (a) retry past a TRANSIENT failure and return the real commit count, and
      (b) on a PERSISTENT failure, raise a CLEAR diagnostic carrying git's stderr — never int('').
    Both simulated by patching subprocess.run the same way the crash-resume test injects a git
    failure (fake CompletedProcess with rc!=0 + empty stdout)."""
    dst = _fresh_repo(tmp_path, "cc")
    real_run = subprocess.run
    expected = int(
        real_run(
            ["git", "-C", _tracker(dst), "rev-list", "--count", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
    )

    # (a) TRANSIENT: fail only the FIRST rev-list, then defer to real git -> retry recovers.
    calls = {"revlist": 0}

    def flaky_run(cmd, *a, **kw):
        if isinstance(cmd, list) and "rev-list" in cmd:
            calls["revlist"] += 1
            if calls["revlist"] == 1:
                return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: transient")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", flaky_run)
    assert _commit_count(dst) == expected  # retried past the injected transient
    assert calls["revlist"] >= 2, "expected a retry after the transient failure"
    monkeypatch.undo()

    # (b) PERSISTENT: every rev-list empties -> a CLEAR error, not the opaque int('') crash.
    def dead_run(cmd, *a, **kw):
        if isinstance(cmd, list) and "rev-list" in cmd:
            return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: boom-42")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", dead_run)
    with pytest.raises(Exception) as ei:  # noqa: B017 — asserting the message shape, any type
        _commit_count(dst)
    msg = str(ei.value)
    assert "invalid literal for int" not in msg, f"still the opaque int('') crash: {msg!r}"
    assert "boom-42" in msg, f"clear error must surface git's stderr, got {msg!r}"


def test_malformed_event_mid_chunk_rolls_back_whole_chunk(tmp_path: Path) -> None:
    """Primitive level: one invalid event in a batch rolls the whole batch back —
    index clean, commit count unchanged, no event files on disk."""
    dst = _fresh_repo(tmp_path, "malformed")
    tracker = _tracker(dst)
    base = _commit_count(dst)

    good = {
        "timestamp": 1700000000000000000,
        "uuid": "u-good",
        "event_type": "COMMENT",
        "env_id": "e",
        "author": "a",
        "data": {"body": "x"},
    }
    bad = {"timestamp": 1, "uuid": "u-bad", "event_type": "NOT_A_TYPE", "data": {}}
    with pytest.raises(event_append.StoreError):
        event_append.batch_stage_and_commit(tracker, [("tk", good), ("tk", bad)])

    assert _commit_count(dst) == base, "no commit written"
    r = subprocess.run(
        ["git", "-C", tracker, "diff", "--cached", "--name-only"], capture_output=True, text=True
    )
    assert r.stdout.strip() == "", "index clean (no phantom staged blob)"
    assert not (Path(tracker) / "tk").exists() or not list((Path(tracker) / "tk").glob("*.json"))


@pytest.mark.integration
def test_crash_before_later_chunk_leaves_whole_commit_or_none_and_resumes(
    tmp_path: Path, monkeypatch
) -> None:
    dst = _fresh_repo(tmp_path, "resume")
    tracker = _tracker(dst)
    recs = _records(1000, with_comment=False)  # CREATE-only: committed events == tickets

    # Fail the 3rd chunk's commit (after K=2 chunks = 512 CREATEs are durably committed).
    real_run = event_append.subprocess.run
    state = {"commits": 0}

    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, list) and "commit" in cmd[:6]:
            state["commits"] += 1
            if state["commits"] == 3:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="injected crash")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(event_append.subprocess, "run", fake_run)
    with pytest.raises(Exception):  # noqa: B017 — the failed chunk aborts the import
        rebar.import_tickets(recs, repo_root=str(dst))
    monkeypatch.undo()

    # Whole-commit-or-none: exactly the first 2 chunks (512 tickets) committed; the
    # failing chunk left nothing (clean index, no partial).
    assert len(rebar.list_tickets(repo_root=str(dst))) == 512
    r = subprocess.run(
        ["git", "-C", tracker, "diff", "--cached", "--name-only"], capture_output=True, text=True
    )
    assert r.stdout.strip() == ""
    after_crash_commits = _commit_count(dst)

    # Re-run the SAME import: source_id re-scan skips the 512 committed, re-emits 488.
    meta = rebar.import_tickets(recs, repo_root=str(dst))
    assert meta["created"] == 488
    assert meta["skipped"] == 512

    # Every source ticket present exactly once (no duplicates / no losses).
    got = {t["source_id"] for t in rebar.list_tickets(repo_root=str(dst))}
    assert got == {r["ticket_id"] for r in recs}
    assert len(rebar.list_tickets(repo_root=str(dst))) == 1000
    # Re-run only added the remaining chunks (ceil(488/256)=2), proving the skip.
    assert _commit_count(dst) - after_crash_commits == math.ceil(488 / 256)
