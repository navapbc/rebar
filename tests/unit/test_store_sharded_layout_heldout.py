from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import rebar
from rebar._commands import _seam
from rebar._ids import resolve_ticket_id
from rebar._snapshot.ticket_view import PinnedTicketView, tracker_head
from rebar._store import event_append, sync
from rebar.reducer import reduce_all_tickets


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _shard(ticket_id: str) -> str:
    return hashlib.sha256(ticket_id.encode()).hexdigest()[:2]


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "user.email", "t@example.com").returncode == 0
    assert _git(repo, "config", "user.name", "Test").returncode == 0
    rebar.init_repo(repo_root=str(repo))


def _write_event(ticket_dir: Path, ticket_id: str, alias: str) -> None:
    event = {
        "timestamp": 1700000000000000000,
        "uuid": f"99999999-0000-4000-8000-{len(ticket_id):012d}",
        "event_type": "CREATE",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "author": "Test",
        "data": {"ticket_type": "task", "title": ticket_id, "alias": alias},
    }
    ticket_dir.mkdir(parents=True, exist_ok=True)
    (ticket_dir / f"{event['timestamp']}-{event['uuid']}-CREATE.json").write_text(
        json.dumps(event, sort_keys=True),
        encoding="utf-8",
    )


def test_resolver_and_replay_work_on_mixed_layout(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    _init_repo(repo)
    tracker = Path(_seam.tracker_dir(str(repo)))

    flat_id = "flat-0001-0001-0001"
    shard_id = "shrd-0002-0002-0002"
    _write_event(tracker / flat_id, flat_id, "flat-alias")
    _write_event(tracker / _shard(shard_id) / shard_id, shard_id, "shard-alias")
    assert _git(tracker, "add", "-A").returncode == 0
    assert _git(tracker, "commit", "-q", "--no-verify", "-m", "mixed").returncode == 0

    assert resolve_ticket_id("flat-alias", str(tracker)) == flat_id
    assert resolve_ticket_id("shard-alias", str(tracker)) == shard_id
    assert rebar.show_ticket(flat_id[:9], repo_root=str(repo))["alias"] == "flat-alias"
    assert rebar.show_ticket(shard_id, repo_root=str(repo))["alias"] == "shard-alias"
    assert {t["ticket_id"] for t in rebar.list_tickets(repo_root=str(repo))} >= {
        flat_id,
        shard_id,
    }


def test_pinned_ticket_view_resolves_sharded_ticket_paths(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    _init_repo(repo)
    tracker = Path(_seam.tracker_dir(str(repo)))

    ticket_id = "view-0003-0003-0003"
    _write_event(tracker / _shard(ticket_id) / ticket_id, ticket_id, "view-alias")
    assert _git(tracker, "add", "-A").returncode == 0
    assert _git(tracker, "commit", "-q", "--no-verify", "-m", "sharded").returncode == 0

    with PinnedTicketView.at_oid(str(tracker), tracker_head(str(tracker))) as view:
        assert view.show_ticket(ticket_id)["ticket_id"] == ticket_id
        assert view.show_ticket("view-alias")["ticket_id"] == ticket_id
        assert view.show_ticket(ticket_id[:9])["ticket_id"] == ticket_id


def test_reconverge_preserves_union_for_sharded_ticket_dirs(tmp_path: Path) -> None:
    origin_tracker = tmp_path / "origin"
    assert origin_tracker.mkdir() is None
    assert _git(origin_tracker, "init", "-q", "-b", "tickets").returncode == 0
    assert _git(origin_tracker, "config", "user.email", "t@example.com").returncode == 0
    assert _git(origin_tracker, "config", "user.name", "Test").returncode == 0
    (origin_tracker / ".keep").write_text("", encoding="utf-8")
    assert _git(origin_tracker, "add", ".keep").returncode == 0
    assert _git(origin_tracker, "commit", "-q", "--no-verify", "-m", "init").returncode == 0
    assert (
        _git(origin_tracker, "config", "receive.denyCurrentBranch", "updateInstead").returncode == 0
    )

    local_tracker = tmp_path / "local"
    assert (
        _git(
            tmp_path, "clone", "-q", "-b", "tickets", str(origin_tracker), str(local_tracker)
        ).returncode
        == 0
    )
    assert _git(local_tracker, "config", "user.email", "t@example.com").returncode == 0
    assert _git(local_tracker, "config", "user.name", "Test").returncode == 0

    origin_id = "orig-0000-0000-0000"
    event_append.stage_and_commit(
        str(origin_tracker),
        origin_id,
        {
            "timestamp": 1700000000000000000,
            "uuid": "11111111-1111-4111-8111-111111111111",
            "event_type": "CREATE",
            "env_id": "00000000-0000-4000-8000-000000000001",
            "author": "Test",
            "data": {"ticket_type": "task", "title": "origin"},
        },
    )
    origin_sha = _git(origin_tracker, "rev-parse", "HEAD").stdout.strip()
    local_id = "locl-0000-0000-0000"
    event_append.stage_and_commit(
        str(local_tracker),
        local_id,
        {
            "timestamp": 1700000000000000001,
            "uuid": "22222222-2222-4222-8222-222222222222",
            "event_type": "CREATE",
            "env_id": "00000000-0000-4000-8000-000000000002",
            "author": "Test",
            "data": {"ticket_type": "task", "title": "local"},
        },
    )
    local_sha = _git(local_tracker, "rev-parse", "HEAD").stdout.strip()

    assert (origin_tracker / _shard(origin_id) / origin_id).is_dir()
    assert (local_tracker / _shard(local_id) / local_id).is_dir()

    sync.reconverge(str(local_tracker))

    assert _git(local_tracker, "merge-base", "--is-ancestor", origin_sha, "HEAD").returncode == 0
    assert _git(local_tracker, "merge-base", "--is-ancestor", local_sha, "HEAD").returncode == 0
    states = {state["ticket_id"]: state for state in reduce_all_tickets(str(local_tracker))}
    assert states[origin_id]["title"] == "origin"
    assert states[local_id]["title"] == "local"
