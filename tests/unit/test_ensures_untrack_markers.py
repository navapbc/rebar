"""S1 (epic becoming-berserk-grunion): untrack per-ticket runtime markers.

Covers the ``untrack-runtime-markers`` ensure unit (``*/.archived`` and
``*/.write.lock`` files committed before .gitignore covered them are removed
from the index — worktree copies untouched — in ONE commit, idempotently) and
the reader self-heal in ``reduce_all_tickets`` (a net-archived ticket whose
``.archived`` marker was deleted by a peer's merge of the untrack commit gets
its marker re-materialized by the next list, while staying excluded from
default output).

Riskiest assumption first: ``git rm --cached`` + commit on the tickets branch
merges cleanly into a peer whose worktree still holds the marker files, and
git's merge deletes them there (two-clone fixture, no rebar store needed).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import init as init_mod
from rebar._store import ensures
from rebar.reducer._api import reduce_all_tickets

_UUID = "3f2a1b4c-5e6d-7f8a-9b0c-1d2e3f4a5b6c"
_UUID2 = "aabbccdd-1122-3344-5566-778899aabbcc"

# The tracker .gitignore lines relevant here (mirrors init._GITIGNORE).
_MARKER_IGNORES = "*/.archived\n*/.write.lock\n"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def _commit_count(cwd: Path) -> int:
    return int(_git(cwd, "rev-list", "--count", "HEAD").stdout.strip())


def _tracked_markers(cwd: Path) -> list[str]:
    out = _git(cwd, "ls-files", "--", "*/.archived", "*/.write.lock").stdout
    return [ln for ln in out.splitlines() if ln]


def _write_event(ticket_dir: Path, timestamp: int, uuid: str, event_type: str, data: dict) -> Path:
    payload = {
        "timestamp": timestamp,
        "uuid": uuid,
        "event_type": event_type,
        "env_id": "00000000-0000-4000-8000-000000000001",
        "author": "Test User",
        "data": data,
    }
    path = ticket_dir / f"{timestamp}-{uuid}-{event_type}.json"
    path.write_text(json.dumps(payload))
    return path


def _plain_tracker(path: Path, *, n_tickets: int = 3) -> Path:
    """A plain git repo shaped like a legacy tickets store: .gitignore covers the
    markers, but ``n_tickets`` ticket dirs have their markers force-added and
    COMMITTED (the pre-gitignore historical state the unit must converge)."""
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    _git(path, "config", "user.email", "t@e")
    _git(path, "config", "user.name", "T")
    (path / ".gitignore").write_text(_MARKER_IGNORES)
    _git(path, "add", ".gitignore")
    marker_paths: list[str] = []
    for i in range(n_tickets):
        d = path / f"aaaa-bbbb-cccc-{i:04d}"
        d.mkdir()
        _write_event(
            d,
            1742605200,
            f"{_UUID[:-4]}{i:04d}",
            "CREATE",
            {"ticket_type": "task", "title": f"t{i}"},
        )
        (d / ".archived").write_text("")
        (d / ".write.lock").write_text("")
        marker_paths += [f"{d.name}/.archived", f"{d.name}/.write.lock"]
    _git(path, "add", "-A")
    _git(path, "add", "-f", "--", *marker_paths)
    commit = _git(path, "commit", "-q", "--no-verify", "-m", "legacy: tracked markers")
    assert commit.returncode == 0
    assert len(_tracked_markers(path)) == 2 * n_tickets
    return path


# ── riskiest assumption: the untrack commit merges cleanly into a peer ────────
def test_untrack_commit_merges_cleanly_into_peer_and_deletes_its_markers(tmp_path: Path) -> None:
    """Peer path: clone B holds the tracked marker files unmodified in its
    worktree and has DIVERGED (its own event commit). A runs the ensure unit;
    B merges the untrack commit. The merge must succeed, git must delete B's
    worktree marker copies (tracked-unmodified files follow the tree deletion),
    and B's status must be clean afterwards."""
    a = _plain_tracker(tmp_path / "a")
    subprocess.run(["git", "clone", "-q", str(a), str(tmp_path / "b")], check=True)
    b = tmp_path / "b"
    _git(b, "config", "user.email", "peer@e")
    _git(b, "config", "user.name", "Peer")
    # Diverge B so the pull is a REAL merge, not a fast-forward.
    _write_event(b / "aaaa-bbbb-cccc-0000", 1742605300, _UUID2, "COMMENT", {"text": "hi"})
    _git(b, "add", "aaaa-bbbb-cccc-0000")
    assert _git(b, "commit", "-q", "--no-verify", "-m", "peer event").returncode == 0

    outcome = init_mod._untrack_runtime_markers_unit(str(a))
    assert outcome.status == "changed"

    assert _git(b, "fetch", "-q", "origin").returncode == 0
    branch = _git(a, "branch", "--show-current").stdout.strip()
    merge = _git(b, "merge", "-q", "--no-edit", f"origin/{branch}")
    assert merge.returncode == 0, merge.stderr
    assert _tracked_markers(b) == []
    # git deleted the peer's worktree marker copies as part of the merge.
    assert not (b / "aaaa-bbbb-cccc-0001" / ".archived").exists()
    assert not (b / "aaaa-bbbb-cccc-0001" / ".write.lock").exists()
    assert _git(b, "status", "--porcelain").stdout == ""


# ── happy path ────────────────────────────────────────────────────────────────
def test_unit_untracks_markers_in_one_commit_keeps_worktree_and_is_idempotent(
    tmp_path: Path,
) -> None:
    tracker = _plain_tracker(tmp_path / "t")
    before = _commit_count(tracker)

    outcome = init_mod._untrack_runtime_markers_unit(str(tracker))

    assert outcome.id == "untrack-runtime-markers"
    assert outcome.status == "changed"
    assert _tracked_markers(tracker) == []
    # Worktree copies are untouched — local cache behavior unchanged.
    assert (tracker / "aaaa-bbbb-cccc-0000" / ".archived").exists()
    assert (tracker / "aaaa-bbbb-cccc-0000" / ".write.lock").exists()
    assert _commit_count(tracker) == before + 1, "exactly ONE commit"
    # Marker churn is now invisible: delete a worktree marker → clean status.
    (tracker / "aaaa-bbbb-cccc-0000" / ".archived").unlink()
    assert _git(tracker, "status", "--porcelain").stdout == ""

    # Second run: converged store, ok, zero commits.
    second = init_mod._untrack_runtime_markers_unit(str(tracker))
    assert second.status == "ok"
    assert _commit_count(tracker) == before + 1


def test_unit_batches_git_rm_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """More marker paths than one batch still all get untracked in ONE commit."""
    tracker = _plain_tracker(tmp_path / "t", n_tickets=5)  # 10 marker files
    monkeypatch.setattr(init_mod, "_UNTRACK_BATCH", 3)
    before = _commit_count(tracker)

    outcome = init_mod._untrack_runtime_markers_unit(str(tracker))

    assert outcome.status == "changed"
    assert _tracked_markers(tracker) == []
    assert _commit_count(tracker) == before + 1


def test_unit_handles_only_one_marker_kind_tracked(tmp_path: Path) -> None:
    """Only .archived tracked (no .write.lock) must not fail on an unmatched
    pathspec — the act enumerates ls-files output, never raw pathspecs."""
    tracker = tmp_path / "t"
    tracker.mkdir()
    subprocess.run(["git", "init", "-q", str(tracker)], check=True)
    _git(tracker, "config", "user.email", "t@e")
    _git(tracker, "config", "user.name", "T")
    d = tracker / "aaaa-bbbb-cccc-0000"
    d.mkdir()
    (d / ".archived").write_text("")
    _git(tracker, "add", "-f", f"{d.name}/.archived")
    _git(tracker, "commit", "-q", "--no-verify", "-m", "legacy")

    outcome = init_mod._untrack_runtime_markers_unit(str(tracker))

    assert outcome.status == "changed"
    assert _tracked_markers(tracker) == []


# ── edge: nothing tracked ─────────────────────────────────────────────────────
def test_fresh_store_reports_ok_and_makes_zero_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh rebar store never tracked the markers: run_ensures on it includes
    the unit, reports non-failed, and the sweep makes zero extra commits."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@e")
    _git(repo, "config", "user.name", "T")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    rebar.init_repo(repo_root=str(repo))
    tracker = repo / ".tickets-tracker"
    before = _commit_count(tracker)

    outcomes = {o.id: o for o in ensures.run_ensures(tracker)}

    assert outcomes["untrack-runtime-markers"].status == "ok"
    assert _commit_count(tracker) == before, "converged sweep must make zero commits"


# ── registry wiring ───────────────────────────────────────────────────────────
def test_unit_is_registered() -> None:
    assert "untrack-runtime-markers" in ensures.REGISTRY_IDS
    assert ensures._registry()["untrack-runtime-markers"] is init_mod._untrack_runtime_markers_unit


# ── reader self-heal: re-materialize a missing .archived marker ───────────────
def test_list_rematerializes_missing_marker_for_net_archived_ticket(tmp_path: Path) -> None:
    """A net-archived ticket whose marker a peer merge deleted stays excluded
    from default list output AND gets its marker re-created by that list call
    (restores the fast path; mirror of the stale-marker clearing)."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    tdir = tracker / "tkt-archived-no-marker"
    tdir.mkdir()
    _write_event(tdir, 1742605200, _UUID, "CREATE", {"ticket_type": "task", "title": "t"})
    _write_event(tdir, 1742605300, _UUID2, "ARCHIVED", {})
    assert not (tdir / ".archived").exists()

    results = reduce_all_tickets(tracker, exclude_archived=True)

    assert all(r.get("ticket_id") != "tkt-archived-no-marker" for r in results)
    assert (tdir / ".archived").exists(), "list must re-materialize the marker"


def test_list_does_not_write_marker_for_unarchived_ticket(tmp_path: Path) -> None:
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    tdir = tracker / "tkt-live"
    tdir.mkdir()
    _write_event(tdir, 1742605200, _UUID, "CREATE", {"ticket_type": "task", "title": "t"})

    results = reduce_all_tickets(tracker, exclude_archived=True)

    assert any(r.get("ticket_id") == "tkt-live" for r in results)
    assert not (tdir / ".archived").exists()


def test_include_archived_list_does_not_touch_markers(tmp_path: Path) -> None:
    """The self-heal is scoped to the exclude_archived (default list) path — an
    ``--include-archived`` style call must not materialize markers."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    tdir = tracker / "tkt-archived-no-marker"
    tdir.mkdir()
    _write_event(tdir, 1742605200, _UUID, "CREATE", {"ticket_type": "task", "title": "t"})
    _write_event(tdir, 1742605300, _UUID2, "ARCHIVED", {})

    results = reduce_all_tickets(tracker, exclude_archived=False)

    assert any(r.get("ticket_id") == "tkt-archived-no-marker" for r in results)
    assert not (tdir / ".archived").exists()


def test_list_still_clears_stale_marker_without_archived_event(tmp_path: Path) -> None:
    """Existing behavior preserved: a marker with NO net ARCHIVED event is stale
    → cleared, and the ticket stays in the results."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    tdir = tracker / "tkt-stale-marker"
    tdir.mkdir()
    _write_event(tdir, 1742605200, _UUID, "CREATE", {"ticket_type": "task", "title": "t"})
    (tdir / ".archived").write_text("")

    results = reduce_all_tickets(tracker, exclude_archived=True)

    assert any(r.get("ticket_id") == "tkt-stale-marker" for r in results)
    assert not (tdir / ".archived").exists(), "stale marker must be cleared"
