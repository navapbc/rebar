"""The purge-bridge deletion commit must not race and drop concurrent locked store writes
(bug 4dd2).

Contract (authoritative):
  * Invariant I5 — "the whole system holds ONE lock" (`src/rebar/_store/lock.py`): every
    store write serializes through the unified write lock.
  * The purge contract (`src/rebar/_commands/purge_bridge.py` `_commit_deletion` docstring):
    the bridge-purge removal MUST commit under the write lock, staging + committing ONLY the
    deleted ticket-dir pathspecs, rather than racing a raw, UNLOCKED, whole-index
    `git add -A` + `git commit`.
  * concurrency.md durability: a committed event is durable and reader-visible; readers list the
    worktree, so a committed event MUST remain a file in the tracker worktree.

`purge_bridge._commit_deletion` used to run a raw, UNLOCKED, whole-index `git add -A` +
`git commit --no-verify`. Concurrently with a locked `append_event` writer it swept that
writer's staged-but-uncommitted event blob into the purge commit (sweep-and-strand data
loss): the writer's own commit then failed (swallowed) and the swept blob was stranded under
the `purge:` message / lost from the worktree. This exercises that exact seam with real git
under real multi-process concurrency and asserts no write is lost. The merged fix
(`write_lock` + pathspec-scoped commit in `_commit_deletion`) makes it pass deterministically.
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

# Contention sized so a losing schedule on the buggy (unlocked, whole-index) commit is
# effectively certain, while the fixed (locked, pathspec-scoped) purge loses nothing
# deterministically. Bounded on purpose: a handful of writers and purgers, small counts, a
# hard communicate() timeout — no CPU hogs, no unbounded loops (mirrors the enrich sibling).
_WRITERS = 5
_BURSTS = 25  # each burst = 1 SIGNATURE + 1 REVIEW_RESULT => 2 events
_PURGERS = 4
_PURGE_TICKETS = 8  # jira-* dirs (re)created + purged per round, spread across purgers
_ROUNDS = 30
_KEEP = "KEEP"  # purge deletes every jira-* dir whose key != KEEP (workers use "DEL-*")
_WORKER = str(Path(__file__).parent / "_purge_race_worker.py")


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


def test_purge_bridge_commit_never_drops_concurrent_locked_writes(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    writer_tids = [rebar.create_ticket("task", f"w{i}", repo_root=store) for i in range(_WRITERS)]
    # Purger jira-* dir base names, spread disjointly across purger processes.
    purge_names = [f"jira-p{i}" for i in range(_PURGE_TICKETS)]
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
                    # instead of only its count. communicate() drains both pipes (no deadlock).
                    stderr=subprocess.PIPE,
                    text=True,
                ),
            )
        )
    for k in range(_PURGERS):
        chunk = purge_names[k::_PURGERS]
        procs.append(
            (
                "purger",
                subprocess.Popen(
                    [sys.executable, _WORKER, "purger", store, _KEEP, str(_ROUNDS), *chunk],
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
        # Diagnostic: dump the exact swallowed write exceptions so a red run identifies the
        # write-loss mechanism. pytest shows captured stdout on failure and hides it when green.
        print("\n=== swallowed writer exceptions ===\n" + "\n\n".join(swallowed_detail))

    assert swallowed_raises == 0 and disk_total == intended_total, (
        "purge-bridge commit raced locked writes: "
        f"intended={intended_total} persisted_on_disk={disk_total} "
        f"lost={intended_total - disk_total} swallowed_write_raises={swallowed_raises}"
    )
