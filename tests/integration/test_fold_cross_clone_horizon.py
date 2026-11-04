"""Two-clone reproduction of the orphaned TAG_DELTA on 77b6-868f (stormproof-stormy-toad).

Production sequence, from the tickets-branch history: the MCP clone wrote a TAG_DELTA and
pushed it; 24 s later the operator clone, which had not fetched since before that write,
folded the ticket. CREATE and LINK were past the 30 min horizon and nothing younger was live
on the operator clone, so the fold placed its SNAPSHOT at the operator's clock. The union merge
then landed the TAG_DELTA before that SNAPSHOT and uncited by it: fsck counts an
``ORPHAN_EVENT`` and replay drops the tag.

Each clone runs as a real child process with its physical clock pinned, so the 24 s gap is
exact and the fold sees only what its clone has fetched.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from _child_hooks import child_env
from _subprocess_env import subprocess_env
from test_concurrency_regression import (  # noqa: F401
    _CLI,
    _create,
    _git,
    _sync_from_origin,
    _tracker,
    _two_clones_template,
    two_clones,
)

from rebar import _engine

_SEC_NS = 1_000_000_000
_MIN_NS = 60 * _SEC_NS
_HORIZON_NS = 30 * _MIN_NS
#: Far above the wall clock, so neither clone's HLC cache outranks the pinned clock.
_T0 = 4_000_000_000_000_000_000


def _run_at(repo: Path, *args: str, now: int) -> subprocess.CompletedProcess[str]:
    env = child_env(
        subprocess_env(_engine.engine_env(repo_root=str(repo)), REBAR_COMPACT_TRIGGER="off"),
        hlc_now=now,
    )
    return subprocess.run(
        [_CLI, *args], cwd=str(repo), env=env, text=True, capture_output=True, check=True
    )


def _events(tracker: Path, tid: str, suffix: str) -> list[Path]:
    return sorted((tracker / tid).glob(f"*-{suffix}.json"))


def _orphan_rows(repo: Path, tid: str) -> list[dict]:
    env = subprocess_env(_engine.engine_env(repo_root=str(repo)), REBAR_COMPACT_TRIGGER="off")
    res = subprocess.run(
        [_CLI, "store", "fsck", "--output", "json"],
        cwd=str(repo),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    issues = json.loads(res.stdout)["issues"]
    return [i for i in issues if i.get("kind") == "orphan_event" and i.get("ticket_id") == tid]


def test_fold_on_stale_clone_keeps_concurrent_tag(two_clones) -> None:  # noqa: F811
    _remote, repo_a, repo_b, _seed = two_clones
    tracker_a, tracker_b = _tracker(repo_a), _tracker(repo_b)

    # Clone A (the operator host) creates the ticket and links it; both writes push.
    tid = _create(repo_a, "task", "fold race victim")
    _run_at(repo_a, "comment", tid, "linked", now=_T0)

    # Clone B (the MCP server) fetches, then tags the ticket 24 s before A's fold.
    fold_at = _T0 + 46 * _MIN_NS
    _sync_from_origin(tracker_b)
    _run_at(repo_b, "edit", tid, "--add-tag=late", now=fold_at - 24 * _SEC_NS)
    (tag_b,) = _events(tracker_b, tid, "TAG_DELTA")
    assert not _events(tracker_a, tid, "TAG_DELTA"), "SETUP: A must not have fetched the tag"

    # A folds without fetching, then receives B's commit through the union merge.
    _run_at(
        repo_a,
        "store",
        "compact",
        tid,
        "--threshold=0",
        f"--horizon={_HORIZON_NS}",
        "--skip-sync",
        now=fold_at,
    )
    (snap,) = _events(tracker_a, tid, "SNAPSHOT")
    _git("fetch", "-q", "origin", "tickets", cwd=tracker_a)
    _git("merge", "-q", "--no-edit", "FETCH_HEAD", cwd=tracker_a)
    assert (tracker_a / tid / tag_b.name).exists(), "SETUP: the merge must land B's tag"

    assert tag_b.name > snap.name, "B's tag must sort after A's SNAPSHOT"
    shown = json.loads(_run_at(repo_a, "show", tid, now=fold_at + _SEC_NS).stdout)
    assert "late" in shown["tags"], shown["tags"]
    assert _orphan_rows(repo_a, tid) == []
