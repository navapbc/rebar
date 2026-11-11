"""RELEASE_DELTA through the event store (ADR 0124, epic inferior-seaborgium-aruanas).

Pins the store half of the ``releases`` field: the write path accepts the event, SNAPSHOT
compaction carries ``releases`` forward, and a reducer cache written before the field
existed (cache version 8) is recomputed instead of served without ``releases``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from _subprocess_env import subprocess_env

import rebar
from rebar._commands import _seam
from rebar.reducer import _cache, reduce_ticket
from rebar.reducer._cache import compute_dir_hash, write_cache


def _tracker(repo: Path) -> Path:
    return _seam.tracker_dir(str(repo))


def _append_release_delta(repo: Path, tid: str, data: dict) -> None:
    _seam.append_event(tid, "RELEASE_DELTA", data, _tracker(repo), repo_root=str(repo))


def test_store_accepts_release_delta(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "Target", repo_root=str(rebar_repo))
    _append_release_delta(rebar_repo, tid, {"added": ["2.4"]})
    _append_release_delta(rebar_repo, tid, {"added": ["2.5"], "removed": ["2.4"]})
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["releases"] == ["2.5"]


def test_releases_survive_compaction(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "Target", repo_root=str(rebar_repo))
    _append_release_delta(rebar_repo, tid, {"added": ["2.4", "2.5"]})
    cp = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "store", "compact", tid, "--threshold=0"],
        capture_output=True,
        text=True,
        cwd=str(rebar_repo),
        env=subprocess_env({"REBAR_SYNC_PULL": "off"}),
        check=False,
    )
    assert cp.returncode == 0, cp.stderr
    snaps = list((_tracker(rebar_repo) / tid).glob("*-SNAPSHOT.json"))
    assert snaps, "expected a SNAPSHOT after compaction"
    assert list((_tracker(rebar_repo) / tid).glob("*-RELEASE_DELTA.json")) == []
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["releases"] == ["2.4", "2.5"]


def test_cache_from_reducer_version_8_is_recomputed(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "Target", repo_root=str(rebar_repo))
    _append_release_delta(rebar_repo, tid, {"added": ["2.4"]})
    tdir = str(_tracker(rebar_repo) / tid)
    events = sorted(n for n in os.listdir(tdir) if n.endswith(".json") and not n.startswith("."))

    # Forge the cache version 8 would have written: a state with no ``releases`` key.
    saved = _cache._REDUCER_CACHE_VERSION
    try:
        _cache._REDUCER_CACHE_VERSION = 8
        old_hash = compute_dir_hash(tdir, events)
    finally:
        _cache._REDUCER_CACHE_VERSION = saved
    stale = reduce_ticket(tdir)
    stale.pop("releases")
    write_cache(str(Path(tdir) / ".cache.json"), old_hash, stale, tdir)

    assert reduce_ticket(tdir)["releases"] == ["2.4"]
