"""The enrich-queue prune must not race and drop concurrent locked store writes
(bug eclectic-spotted-barb).

Contract (authoritative):
  * Invariant I5 — "the whole system holds ONE lock" (`src/rebar/_store/lock.py`): every
    store write serializes through the unified write lock.
  * The prune contract (`src/rebar/_store/event_append.py` `delete_events` docstring): a
    sidecar/queue prune MUST route through the locked, pathspec-scoped `delete_events`
    rather than racing a raw `git rm` + whole-index `git commit`.
  * CLAUDE.md durability: a committed event is durable and reader-visible; readers list the
    worktree, so a committed event MUST remain a file in the tracker worktree.

`rebar.llm.enrich_drain._prune_queue_events` ran a raw, UNLOCKED, whole-index `git rm` +
`git commit` in the `[agents]` drain child. Concurrently with a review's locked
`append_event` writes it swept/orphaned the review's staged SIGNATURE/REVIEW_RESULT blob:
the writer's own commit then failed (swallowed by `sidecar.emit` -> `sidecar_emitted=False`
and by `attest.sign_plan_review` -> unsigned), and the swept blob was unlinked from the
worktree though it lived in HEAD. This exercises that exact seam with real git under real
multi-process concurrency and asserts no write is lost.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import rebar
from rebar import config

pytestmark = pytest.mark.integration

# Contention sized so a losing schedule on the buggy (unlocked) code is effectively certain
# (~9% per-write loss observed => P(no loss) ~ e^-20), while the fixed (locked) prune loses
# nothing deterministically.
_WRITERS = 5
_BURSTS = 25  # each burst = 1 SIGNATURE + 1 REVIEW_RESULT => 2 events
_PRUNERS = 4
_PRUNER_TICKETS = 8
_ROUNDS = 30
_WORKER = str(Path(__file__).parent / "_prune_race_worker.py")


def _fresh_store(tmp_path: Path) -> str:
    repo = tmp_path / "store"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    rebar.init_repo(repo_root=str(repo))
    return str(repo)


def _disk_events(tracker: str, tid: str) -> int:
    d = Path(tracker) / tid
    if not d.is_dir():
        return 0
    return sum(
        1
        for f in os.listdir(d)
        if f.endswith("-SIGNATURE.json") or f.endswith("-REVIEW_RESULT.json")
    )


def test_enrich_queue_prune_never_drops_concurrent_locked_writes(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    writer_tids = [rebar.create_ticket("task", f"w{i}", repo_root=store) for i in range(_WRITERS)]
    pruner_tids = [
        rebar.create_ticket("task", f"p{i}", repo_root=store) for i in range(_PRUNER_TICKETS)
    ]
    tracker = str(config.tracker_dir(store))

    procs: list[tuple[str, subprocess.Popen]] = []
    for tid in writer_tids:
        procs.append(
            (
                "writer",
                subprocess.Popen(
                    [sys.executable, _WORKER, "writer", store, tid, str(_BURSTS)],
                    stdout=subprocess.PIPE,
                    # Capture writer stderr so a red run REVEALS the swallowed-write mechanism
                    # (bug ac26 residual) instead of only its count. communicate() drains both
                    # pipes, so no fill-deadlock.
                    stderr=subprocess.PIPE,
                    text=True,
                ),
            )
        )
    for k in range(_PRUNERS):
        chunk = pruner_tids[k::_PRUNERS]
        procs.append(
            (
                "pruner",
                subprocess.Popen(
                    [sys.executable, _WORKER, "pruner", store, str(_ROUNDS), *chunk],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                ),
            )
        )

    swallowed_raises = 0
    swallowed_detail: list[str] = []
    for role, p in procs:
        out, err = p.communicate(timeout=180)
        if role == "writer":
            if out:
                swallowed_raises += int(out.strip() or "0")
            if err and err.strip():
                swallowed_detail.append(err.strip())

    # Oracle: readers list the worktree, so every intended event MUST be a worktree file.
    intended_per_writer = _BURSTS * 2
    disk_total = sum(_disk_events(tracker, tid) for tid in writer_tids)
    intended_total = _WRITERS * intended_per_writer

    if swallowed_detail:
        # Diagnostic (bug ac26 residual): dump the exact swallowed write exceptions so a red
        # CI run identifies the residual write-loss mechanism (uncovered git-failure signature
        # vs OOM-killed subprocess) rather than only counting it. pytest shows captured stdout
        # on failure and hides it on a green run.
        print("\n=== swallowed writer exceptions (bug ac26) ===\n" + "\n\n".join(swallowed_detail))

    assert swallowed_raises == 0 and disk_total == intended_total, (
        "enrich-queue prune raced locked writes: "
        f"intended={intended_total} persisted_on_disk={disk_total} "
        f"lost={intended_total - disk_total} swallowed_write_raises={swallowed_raises}"
    )
