"""Ticket-directory creation is atomic: an interrupted create leaves no debris (021d).

The debris signature this pins: the write path used to ``os.makedirs`` the ticket
directory OUTSIDE the write lock and only land its first event at the under-lock
``os.replace``. Death anywhere in that window (host sleep, kill, lock timeout) stranded an
empty, plausible-looking ticket directory that ``fsck`` reports TWICE — ``MISSING_CREATE``
(the reducer finds no CREATE) and ``FOREIGN_STORE_PATH`` (the directory holds no event
file). A real sweep of 8 such directories is recorded on ticket illsuited-erect-ibis.

The interruption is injected at the write lock, which sits INSIDE the vulnerable window and
is a genuine failure mode. It is deliberately shape-independent — it fails the same call in
the old shape and the new one — so RED and GREEN are compared honestly rather than by
patching a symbol only one shape happens to call.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest

import rebar
from rebar._commands import fsck as _fsck
from rebar._store import event_append, lock_owner, staging
from rebar._store.lock import LockTimeout


def _tracker(repo: Path) -> str:
    return str(repo / ".tickets-tracker")


def _event(event_type: str = "CREATE") -> dict[str, object]:
    return {
        "timestamp": time.time_ns(),
        "uuid": str(uuid.uuid4()),
        "event_type": event_type,
        "env_id": "test-env",
        "author": "tester",
        "data": {"ticket_type": "task", "title": "atomic create"},
    }


def _dead_owner_stamp() -> str:
    """This host's own stamp, re-pointed at a pid that is provably not running.

    Built from the live stamp so host identity and pid namespace still match — otherwise
    the probe would report "unprobeable" and the sweep would decline for the wrong reason,
    and the test would pass without exercising the dead-owner verdict at all."""
    pid = 300000
    while lock_owner._pid_alive(pid):
        pid -= 1
    return " ".join(
        f"pid={pid}" if token.startswith("pid=") else token
        for token in lock_owner._owner_stamp().split()
    )


def _events_in(ticket_dir: str) -> list[str]:
    """Event files in *ticket_dir*, ignoring the reducer's own dot-prefixed cache."""
    return sorted(n for n in os.listdir(ticket_dir) if not n.startswith("."))


def _boom_lock(*_args: object, **_kwargs: object) -> object:
    raise LockTimeout("Error: could not acquire ticket write lock")


def _fsck_findings(tracker: str, ticket_id: str) -> list[str]:
    """Every fsck finding naming *ticket_id* — the debris signature, if any."""
    findings: list[str] = []
    lines, _count = _fsck._scan(tracker, True)
    findings += [ln for ln in lines if ticket_id in ln]
    if ticket_id in _fsck.foreign_store_path_list(tracker):
        findings.append(f"FOREIGN_STORE_PATH: {ticket_id}")
    return findings


def test_interrupted_create_leaves_no_fsck_visible_debris(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1/AC2 — the RED case: interrupt a create, expect NO stray ticket directory.

    Against the old ``makedirs``-then-rename shape this fails with the exact production
    signature: MISSING_CREATE + FOREIGN_STORE_PATH for an empty directory."""
    rebar.create_ticket("task", "seed", repo_root=str(rebar_repo))
    tracker = _tracker(rebar_repo)
    ticket_id = "0efe-9207-2706-43a3"

    monkeypatch.setattr(event_append._lock, "write_lock", _boom_lock)
    with pytest.raises(LockTimeout):
        event_append.stage_and_commit(tracker, ticket_id, _event())

    assert not os.path.exists(os.path.join(tracker, ticket_id)), (
        "the interrupted create stranded a ticket directory — the debris this fix removes"
    )
    assert _fsck_findings(tracker, ticket_id) == []


def test_interrupted_create_leaves_only_an_invisible_staging_path(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1/AC2 — whatever the interruption strands must be invisible to both fsck checks.

    Discard is suppressed to model a process that DIES before its cleanup runs (a SIGKILL
    covers no ``finally``), which is the only residue an interruption can leave."""
    rebar.create_ticket("task", "seed", repo_root=str(rebar_repo))
    tracker = _tracker(rebar_repo)
    ticket_id = "2021-2ef0-e36f-44fc"

    monkeypatch.setattr(staging.StagedEvent, "discard", lambda self: None)
    monkeypatch.setattr(event_append._lock, "write_lock", _boom_lock)
    with pytest.raises(LockTimeout):
        event_append.stage_and_commit(tracker, ticket_id, _event())

    stranded = [n for n in os.listdir(tracker) if n.startswith(staging.STAGING_PREFIX)]
    assert stranded, "the staging path under test was never created"
    # The whole point of the naming convention: neither check can see it.
    lines, _count = _fsck._scan(tracker, True)
    assert [ln for ln in lines if staging.STAGING_PREFIX in ln] == []
    assert [p for p in _fsck.foreign_store_path_list(tracker) if p.startswith(".")] == []
    assert _fsck_findings(tracker, ticket_id) == []


def test_successful_create_publishes_dir_and_event_together(rebar_repo: Path) -> None:
    """The happy path still lands the event, and leaves no staging residue."""
    rebar.create_ticket("task", "seed", repo_root=str(rebar_repo))
    tracker = _tracker(rebar_repo)
    ticket_id = "6f8a-45e5-d9ec-402c"
    event = _event()

    assert event_append.stage_and_commit(tracker, ticket_id, event) == 0

    landed = _events_in(os.path.join(tracker, ticket_id))
    assert landed == [event_append.event_filename(event["timestamp"], event["uuid"], "CREATE")], (
        f"the published ticket directory holds unexpected contents: {landed}"
    )
    assert [n for n in os.listdir(tracker) if n.startswith(staging.STAGING_PREFIX)] == []
    assert _fsck_findings(tracker, ticket_id) == []


def test_second_event_for_an_existing_ticket_is_unchanged(rebar_repo: Path) -> None:
    """Only the FIRST event stages a directory; later events keep the file-staging path."""
    rebar.create_ticket("task", "seed", repo_root=str(rebar_repo))
    tracker = _tracker(rebar_repo)
    ticket_id = "a109-e758-71bb-4371"

    event_append.stage_and_commit(tracker, ticket_id, _event())
    event_append.stage_and_commit(tracker, ticket_id, _event("COMMENT"))

    assert len(_events_in(os.path.join(tracker, ticket_id))) == 2
    assert _fsck_findings(tracker, ticket_id) == []


def test_interrupted_batch_create_leaves_no_debris(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4 — ``batch_stage_and_commit`` shares ``_prepare_event`` and the same window."""
    rebar.create_ticket("task", "seed", repo_root=str(rebar_repo))
    tracker = _tracker(rebar_repo)
    ids = ["afe5-db0b-e9c5-45d8", "c827-81ee-a592-4d27"]

    monkeypatch.setattr(event_append._lock, "write_lock", _boom_lock)
    with pytest.raises(LockTimeout):
        event_append.batch_stage_and_commit(tracker, [(t, _event()) for t in ids])

    for ticket_id in ids:
        assert not os.path.exists(os.path.join(tracker, ticket_id))
        assert _fsck_findings(tracker, ticket_id) == []


def test_batch_create_publishes_every_new_ticket(rebar_repo: Path) -> None:
    rebar.create_ticket("task", "seed", repo_root=str(rebar_repo))
    tracker = _tracker(rebar_repo)
    ids = ["d052-ad3f-23c1-4511", "fa38-670c-d8fc-4c22"]

    assert event_append.batch_stage_and_commit(tracker, [(t, _event()) for t in ids]) == 2

    for ticket_id in ids:
        assert len(_events_in(os.path.join(tracker, ticket_id))) == 1
        assert _fsck_findings(tracker, ticket_id) == []
    assert [n for n in os.listdir(tracker) if n.startswith(staging.STAGING_PREFIX)] == []


def test_concurrent_creates_get_distinct_staging_paths(rebar_repo: Path) -> None:
    """AC3 — staging names are pid+uuid suffixed, so two in-flight creates cannot collide."""
    rebar.create_ticket("task", "seed", repo_root=str(rebar_repo))
    tracker = _tracker(rebar_repo)

    first = staging.stage_event(tracker, "1111-1111-1111-1111", "e1.json", b"{}")
    second = staging.stage_event(tracker, "2222-2222-2222-2222", "e2.json", b"{}")
    try:
        assert first.staging_dir is not None and second.staging_dir is not None
        assert first.staging_dir != second.staging_dir
        assert str(os.getpid()) in os.path.basename(first.staging_dir)
        first.promote()
        second.promote()
        assert os.path.exists(first.final_path) and os.path.exists(second.final_path)
    finally:
        first.discard()
        second.discard()


def test_sweep_removes_dead_owner_staging_but_spares_a_live_one(rebar_repo: Path) -> None:
    """AC2 — the bounded sweep reclaims abandoned staging paths only."""
    rebar.create_ticket("task", "seed", repo_root=str(rebar_repo))
    tracker = _tracker(rebar_repo)

    dead = staging._new_staging_path(tracker)
    os.mkdir(dead)
    staging._write_owner_stamp(dead, stamp=_dead_owner_stamp())
    live = staging._new_staging_path(tracker)
    os.mkdir(live)
    staging._write_owner_stamp(live)
    seed_dirs = sorted(n for n in os.listdir(tracker) if not n.startswith("."))

    staging.sweep_stale_staging(tracker)

    assert not os.path.exists(dead), "an abandoned staging path was not reclaimed"
    assert os.path.exists(live), "the sweep removed a LIVE writer's staging path"
    # 043f boundary: the sweep touches the writer's own staging area, never store data.
    assert sorted(n for n in os.listdir(tracker) if not n.startswith(".")) == seed_dirs


def test_sweep_removes_recycled_pid_staging(rebar_repo: Path, monkeypatch) -> None:
    """A live pid with a differing known start time is not the staging owner."""
    rebar.create_ticket("task", "seed", repo_root=str(rebar_repo))
    tracker = _tracker(rebar_repo)
    monkeypatch.setattr(lock_owner, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(lock_owner, "_process_start_time", lambda _pid: "222")
    recycled = staging._new_staging_path(tracker)
    os.mkdir(recycled)
    staging._write_owner_stamp(
        recycled,
        stamp=(
            f"{lock_owner._STAMP_V2_PREFIX} host={lock_owner._host_identity()} "
            f"ns={lock_owner._read_pid_namespace_id() or lock_owner._STAMP_UNKNOWN} "
            "pid=4321 start=111"
        ),
    )

    staging.sweep_stale_staging(tracker)

    assert not os.path.exists(recycled), "the sweep kept a recycled-pid staging owner"


def test_sweep_never_touches_ticket_directories(rebar_repo: Path) -> None:
    """043f — an event-less ticket directory is tolerated, never tidied, by this sweep."""
    rebar.create_ticket("task", "seed", repo_root=str(rebar_repo))
    tracker = _tracker(rebar_repo)
    orphan = os.path.join(tracker, "0000-1111-2222-3333")
    os.mkdir(orphan)

    staging.sweep_stale_staging(tracker)

    assert os.path.isdir(orphan), (
        "the sweep deleted a ticket directory — 043f rules that debris is tolerated, "
        "never tidied, by the writer"
    )
