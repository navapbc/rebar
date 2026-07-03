"""P1.1: the derived ``updated_at`` field.

``updated_at`` is the max event timestamp shaping a ticket — computed during
replay, surfaced on the reduced/public state, and used by ``--sort updated``. It
is NEVER persisted into the event log: ``compact.py`` strips it before writing a
SNAPSHOT's ``compiled_state``, so SNAPSHOT bytes stay byte-identical to pre-P1.1
and a re-derive on replay is the single source of truth (decision R2.1/R2.2).
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from pathlib import Path

import rebar


def _snapshot_compiled_state(repo: Path, tid: str) -> dict:
    snaps = glob.glob(str(repo / ".tickets-tracker" / tid / "*-SNAPSHOT.json"))
    assert snaps, "expected a SNAPSHOT after compaction"
    snaps.sort()
    return json.loads(Path(snaps[-1]).read_text())


def _compact(repo: Path, tid: str) -> None:
    env = dict(os.environ)
    env["REBAR_ROOT"] = str(repo)
    env["REBAR_SYNC_PULL"] = "off"
    cp = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "compact", tid, "--threshold=0"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
    )
    assert cp.returncode == 0, cp.stderr


def test_updated_at_present_and_at_least_created_at(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "freshly created")
    st = rebar.show_ticket(tid)
    assert "updated_at" in st
    assert st["updated_at"] is not None
    # A brand-new ticket: updated_at == created_at (the CREATE event timestamp).
    assert st["updated_at"] == st["created_at"]


def test_updated_at_advances_on_later_events(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "will be edited")
    created = rebar.show_ticket(tid)["created_at"]
    rebar.comment(tid, "a later event")
    updated = rebar.show_ticket(tid)["updated_at"]
    assert updated > created


def test_snapshot_compiled_state_excludes_updated_at(rebar_repo: Path) -> None:
    # Byte-parity guard: the derived field must not ride into event-log bytes.
    tid = rebar.create_ticket("task", "to be compacted")
    rebar.comment(tid, "c1")
    rebar.comment(tid, "c2")
    _compact(rebar_repo, tid)
    compiled = _snapshot_compiled_state(rebar_repo, tid)["data"]["compiled_state"]
    assert "updated_at" not in compiled
    # created_at is a real persisted field and MUST survive.
    assert "created_at" in compiled


def test_compacted_then_untouched_reports_compacted_at(rebar_repo: Path) -> None:
    # After compaction with no later events, updated_at re-seeds from the
    # SNAPSHOT's compacted_at (replay skips the pre-snapshot events it folds in).
    tid = rebar.create_ticket("task", "compact me")
    rebar.comment(tid, "c1")
    rebar.comment(tid, "c2")
    _compact(rebar_repo, tid)
    snap = _snapshot_compiled_state(rebar_repo, tid)
    compacted_at = snap["data"]["compacted_at"]
    assert rebar.show_ticket(tid)["updated_at"] == compacted_at


def test_stale_pre_p1_1_cache_is_invalidated(rebar_repo: Path) -> None:
    # A .cache.json written before updated_at existed (an OLDER reducer-cache
    # version) must be invalidated by the version bump, not served verbatim —
    # otherwise every untouched ticket would report updated_at=None post-upgrade.
    import os

    from rebar.reducer import _cache
    from rebar.reducer._api import reduce_ticket
    from rebar.reducer._cache import compute_dir_hash, write_cache

    tid = rebar.create_ticket("task", "cache check")
    tdir = str(rebar_repo / ".tickets-tracker" / tid)
    events = sorted(f for f in os.listdir(tdir) if f.endswith(".json") and not f.startswith("."))

    # Forge a cache as the PREVIOUS version (2) would have: a reduced state with
    # NO updated_at, keyed by the dir-hash that version 2 produced.
    old_version = 2
    saved = _cache._REDUCER_CACHE_VERSION
    try:
        _cache._REDUCER_CACHE_VERSION = old_version
        old_hash = compute_dir_hash(tdir, events)
        stale_state = {k: v for k, v in reduce_ticket(tdir).items() if k != "updated_at"}
    finally:
        _cache._REDUCER_CACHE_VERSION = saved
    write_cache(str(Path(tdir) / ".cache.json"), old_hash, stale_state, tdir)

    # Under the current version the old hash no longer matches → cache miss →
    # updated_at re-derived rather than served as None.
    st = reduce_ticket(tdir)
    assert st.get("updated_at") is not None


def test_updated_at_survives_post_snapshot_event(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "compact then touch")
    rebar.comment(tid, "c1")
    _compact(rebar_repo, tid)
    compacted_at = _snapshot_compiled_state(rebar_repo, tid)["data"]["compacted_at"]
    rebar.comment(tid, "post-snapshot comment")
    # A post-snapshot event must push updated_at beyond compacted_at.
    assert rebar.show_ticket(tid)["updated_at"] > compacted_at
