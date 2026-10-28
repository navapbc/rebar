"""Converge SNAPSHOTs that froze close metadata a later reopen should have cleared (df72).

Bug 6df4-d3f0 fixed the reducer fold so a ``closed -> *`` edge drops ``close_class``,
``close_reason``, ``force_close_reason`` and ``completion_expectation``. Replay starts at the
latest SNAPSHOT and never re-folds the retired events behind it, so a ticket compacted before
that fix keeps the stale values forever. The ``snapshot-close-metadata`` ensure unit rebuilds
exactly those tickets from their full log.

The stale SNAPSHOT is fabricated by writing the pre-fix field into a correctly compacted one:
the fixed fold can no longer produce it, and that frozen field is the whole defect.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands._seam import tracker_dir
from rebar._store import ensures
from rebar._store.snapshot_ensures import (
    CLOSE_METADATA_KEYS,
    converge_snapshot_close_metadata_unit,
)

pytestmark = pytest.mark.unit

_UNIT_ID = "snapshot-close-metadata"
_STALE = {
    "force_close_reason": "operator: forced before the reopen",
    "close_class": "wontfix",
    "completion_expectation": "force_bypassed",
}


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "dev@example.com"),
        ("git", "config", "user.name", "Dev Example"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _tracker(store: Path) -> str:
    return str(tracker_dir(str(store)))


def _commit_count(store: Path) -> int:
    out = subprocess.run(
        ["git", "-C", _tracker(store), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout.strip())


def _active_snapshot(store: Path, tid: str) -> Path:
    tdir = Path(_tracker(store)) / tid
    names = sorted(n for n in os.listdir(tdir) if n.endswith("-SNAPSHOT.json"))
    assert names, f"{tid} has no active SNAPSHOT"
    return tdir / names[-1]


def _freeze(store: Path, tid: str, fields: dict[str, str]) -> None:
    """Write pre-fix close metadata into the ticket's latest SNAPSHOT (and commit it)."""
    path = _active_snapshot(store, tid)
    event = json.loads(path.read_text())
    event["data"]["compiled_state"].update(fields)
    path.write_text(json.dumps(event))
    subprocess.run(["git", "-C", _tracker(store), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", _tracker(store), "commit", "-q", "--no-verify", "-m", "fixture: freeze"],
        check=True,
        capture_output=True,
    )


def _state(store: Path, tid: str) -> dict:
    return rebar.show_ticket(tid, repo_root=str(store))


def _forced_close(store: Path, tid: str) -> None:
    rebar.claim(tid, assignee="agent", repo_root=str(store))
    rebar.transition(
        tid,
        "in_progress",
        "closed",
        force="operator: forced before the reopen",
        repo_root=str(store),
    )


def _reopened_ticket(store: Path, title: str, *, reclosed: bool) -> str:
    tid = rebar.create_ticket("task", title, repo_root=str(store))
    _forced_close(store, tid)
    rebar.reopen(tid, repo_root=str(store))
    if reclosed:
        rebar.claim(tid, assignee="agent", repo_root=str(store))
        rebar.transition(tid, "in_progress", "closed", repo_root=str(store))
    rebar.compact(tid, repo_root=str(store))
    _freeze(store, tid, _STALE)
    return tid


def _has_close_metadata(state: dict) -> bool:
    return any(state.get(k) for k in CLOSE_METADATA_KEYS)


def test_snapshot_close_metadata_fixture_reproduces_the_stale_replay(store: Path) -> None:
    """Precondition: replay serves the frozen reason although the ticket was reopened."""
    tid = _reopened_ticket(store, "reopened", reclosed=False)

    state = _state(store, tid)

    assert state["status"] == "open"
    assert state["force_close_reason"] == _STALE["force_close_reason"]


def test_snapshot_close_metadata_unit_clears_a_reopened_ticket(store: Path) -> None:
    tid = _reopened_ticket(store, "reopened", reclosed=False)

    outcome = converge_snapshot_close_metadata_unit(_tracker(store))

    assert (outcome.id, outcome.status) == (_UNIT_ID, "changed")
    state = _state(store, tid)
    assert state["status"] == "open"
    assert not _has_close_metadata(state)


def test_snapshot_close_metadata_unit_clears_a_reclosed_ticket(store: Path) -> None:
    """A clean re-close after the forced one must not keep the forced close's reason."""
    tid = _reopened_ticket(store, "reclosed", reclosed=True)

    outcome = converge_snapshot_close_metadata_unit(_tracker(store))

    assert outcome.status == "changed"
    state = _state(store, tid)
    assert state["status"] == "closed"
    assert not state.get("force_close_reason")
    assert state.get("close_class") != "wontfix"


def test_snapshot_close_metadata_unit_leaves_a_still_forced_closed_ticket_alone(
    store: Path,
) -> None:
    """Never reopened: the forced reason is current, so it is neither cleared nor rebuilt."""
    tid = rebar.create_ticket("task", "still forced", repo_root=str(store))
    _forced_close(store, tid)
    rebar.compact(tid, repo_root=str(store))
    snapshot = _active_snapshot(store, tid)
    before = _state(store, tid)["force_close_reason"]

    outcome = converge_snapshot_close_metadata_unit(_tracker(store))

    assert outcome.status == "ok"
    assert _state(store, tid)["force_close_reason"] == before
    assert _active_snapshot(store, tid) == snapshot, "an already-correct SNAPSHOT was rewritten"


def test_snapshot_close_metadata_unit_is_idempotent_with_zero_commits_when_converged(
    store: Path,
) -> None:
    _reopened_ticket(store, "a", reclosed=False)
    _reopened_ticket(store, "b", reclosed=True)
    assert converge_snapshot_close_metadata_unit(_tracker(store)).status == "changed"
    commits = _commit_count(store)

    again = converge_snapshot_close_metadata_unit(_tracker(store))

    assert again.status == "ok"
    assert _commit_count(store) == commits, "a converged store must make zero commits"


def test_snapshot_close_metadata_unit_runs_under_the_sweep_lock_without_deadlock(
    store: Path,
) -> None:
    """The sweep holds the write lock; the unit must rebuild without re-acquiring it."""
    tid = _reopened_ticket(store, "swept", reclosed=False)

    outcomes = {o.id: o for o in ensures.run_ensures(_tracker(store), timeout=2, attempts=1)}

    assert outcomes[_UNIT_ID].status == "changed"
    assert not _has_close_metadata(_state(store, tid))


def test_snapshot_close_metadata_unit_is_registered() -> None:
    assert _UNIT_ID in ensures.REGISTRY_IDS
    assert ensures._registry()[_UNIT_ID] is converge_snapshot_close_metadata_unit
