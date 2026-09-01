from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import _seam


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _init_tracker(path: Path) -> None:
    path.mkdir(parents=True)
    assert _git(path, "init", "-q", "-b", "tickets").returncode == 0
    assert _git(path, "config", "user.email", "t@example.com").returncode == 0
    assert _git(path, "config", "user.name", "Test").returncode == 0


def _commit_all(path: Path, message: str) -> str:
    assert _git(path, "add", "-A").returncode == 0
    commit = _git(path, "commit", "-q", "--no-verify", "-m", message)
    assert commit.returncode == 0, commit.stderr
    return _git(path, "rev-parse", "HEAD").stdout.strip()


def _tree_size(path: Path, spec: str) -> int:
    cp = _git(path, "cat-file", "-s", spec)
    assert cp.returncode == 0, cp.stderr
    return int(cp.stdout)


def _ticket_id(n: int) -> str:
    return f"t{n:03d}-aa{n % 100:02d}-bb{(n * 7) % 100:02d}-cc{(n * 13) % 100:02d}"


def _shard(ticket_id: str) -> str:
    return hashlib.sha256(ticket_id.encode()).hexdigest()[:2]


def _write_create(ticket_dir: Path, ticket_id: str, alias: str) -> None:
    event = {
        "timestamp": 1700000000000000000,
        "uuid": f"00000000-0000-4000-8000-{abs(hash(ticket_id)) % 10**12:012d}",
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


def _populate_layout(path: Path, *, sharded: bool, count: int = 2048) -> str:
    _init_tracker(path)
    target = _ticket_id(count // 2)
    for n in range(count):
        tid = _ticket_id(n)
        parent = path / _shard(tid) / tid if sharded else path / tid
        _write_create(parent, tid, f"alias-{n}")
    _commit_all(path, "seed tickets")
    marker = "1700000000000000001-11111111-1111-4111-8111-111111111111-COMMENT.json"
    ticket_dir = path / _shard(target) / target if sharded else path / target
    (ticket_dir / marker).write_text('{"event_type":"COMMENT"}', encoding="utf-8")
    _commit_all(path, "mutate one ticket")
    return target


def test_sharded_layout_reduces_reemitted_tree_bytes(tmp_path: Path) -> None:
    flat = tmp_path / "flat"
    sharded = tmp_path / "sharded"

    target = _populate_layout(flat, sharded=False)
    assert _populate_layout(sharded, sharded=True) == target

    flat_reemitted = _tree_size(flat, "HEAD^{tree}")
    sharded_reemitted = _tree_size(sharded, "HEAD^{tree}") + _tree_size(
        sharded, f"HEAD:{_shard(target)}"
    )

    assert sharded_reemitted < flat_reemitted * 0.20


def test_new_ticket_write_uses_sha256_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    assert _git(repo, "config", "user.email", "t@example.com").returncode == 0
    assert _git(repo, "config", "user.name", "Test").returncode == 0
    monkeypatch.chdir(repo)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))

    ticket_id = rebar.create_ticket("task", "sharded", repo_root=str(repo))
    tracker = Path(_seam.tracker_dir(str(repo)))

    assert (tracker / _shard(ticket_id) / ticket_id).is_dir()
    assert not (tracker / ticket_id).exists()


def test_migration_requires_operator_opt_in_for_flat_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._store.ticket_layout import (
        TICKET_LAYOUT_CUTOVER_ENV,
        migrate_flat_ticket_dirs_unit,
        ticket_dir,
    )

    tracker = tmp_path / "tracker"
    _init_tracker(tracker)
    ticket_id = _ticket_id(0)
    _write_create(tracker / ticket_id, ticket_id, "alias-0")
    head = _commit_all(tracker, "flat seed")

    monkeypatch.delenv(TICKET_LAYOUT_CUTOVER_ENV, raising=False)
    outcome = migrate_flat_ticket_dirs_unit(str(tracker))

    assert outcome.status == "failed"
    assert "pending operator cutover" in outcome.detail
    assert _git(tracker, "rev-parse", "HEAD").stdout.strip() == head
    assert (tracker / ticket_id).is_dir()
    assert not (tracker / _shard(ticket_id) / ticket_id).exists()
    assert ticket_dir(str(tracker), _ticket_id(1)) == str(tracker / _ticket_id(1))


def test_migration_preserves_identity_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._store.ticket_layout import (
        TICKET_LAYOUT_CUTOVER_ENV,
        migrate_flat_ticket_dirs_unit,
        ticket_dir,
    )

    tracker = tmp_path / "tracker"
    _init_tracker(tracker)
    expected = {}
    for n in range(4):
        tid = _ticket_id(n)
        alias = f"alias-{n}"
        _write_create(tracker / tid, tid, alias)
        expected[tid] = alias
    _commit_all(tracker, "flat seed")

    monkeypatch.setenv(TICKET_LAYOUT_CUTOVER_ENV, "1")
    outcome = migrate_flat_ticket_dirs_unit(str(tracker))
    assert outcome.status == "changed"

    for tid, alias in expected.items():
        assert not (tracker / tid).exists()
        migrated = ticket_dir(str(tracker), tid)
        assert migrated == str(tracker / _shard(tid) / tid)
        state = rebar.reducer.reduce_ticket(migrated)
        assert state is not None
        assert state["ticket_id"] == tid
        assert state["alias"] == alias

    head = _git(tracker, "rev-parse", "HEAD").stdout.strip()
    second = migrate_flat_ticket_dirs_unit(str(tracker))
    assert second.status == "ok"
    assert _git(tracker, "rev-parse", "HEAD").stdout.strip() == head


def test_migration_resumes_partially_sharded_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._store.ticket_layout import (
        TICKET_LAYOUT_CUTOVER_ENV,
        migrate_flat_ticket_dirs_unit,
        ticket_dir,
    )

    tracker = tmp_path / "tracker"
    _init_tracker(tracker)
    first, second = _ticket_id(1), _ticket_id(2)
    _write_create(tracker / first, first, "first")
    _write_create(tracker / second, second, "second")
    _commit_all(tracker, "flat seed")
    (tracker / _shard(first)).mkdir()
    shutil.move(str(tracker / first), tracker / _shard(first) / first)

    monkeypatch.setenv(TICKET_LAYOUT_CUTOVER_ENV, "1")
    outcome = migrate_flat_ticket_dirs_unit(str(tracker))

    assert outcome.status == "changed"
    for tid in (first, second):
        assert Path(ticket_dir(str(tracker), tid)).is_dir()
        assert rebar.reducer.reduce_ticket(ticket_dir(str(tracker), tid))["ticket_id"] == tid
    assert _git(tracker, "diff", "--quiet").returncode == 0
    assert _git(tracker, "diff", "--cached", "--quiet").returncode == 0


def test_migration_commits_fully_moved_partial_worktree(tmp_path: Path) -> None:
    from rebar._store.ticket_layout import migrate_flat_ticket_dirs_unit, ticket_dir

    tracker = tmp_path / "tracker"
    _init_tracker(tracker)
    tickets = [_ticket_id(1), _ticket_id(2)]
    for ticket_id in tickets:
        _write_create(tracker / ticket_id, ticket_id, ticket_id)
    _commit_all(tracker, "flat seed")
    for ticket_id in tickets:
        (tracker / _shard(ticket_id)).mkdir(exist_ok=True)
        shutil.move(str(tracker / ticket_id), tracker / _shard(ticket_id) / ticket_id)

    outcome = migrate_flat_ticket_dirs_unit(str(tracker))

    assert outcome.status == "changed"
    for ticket_id in tickets:
        migrated = ticket_dir(str(tracker), ticket_id)
        assert Path(migrated).is_dir()
        assert rebar.reducer.reduce_ticket(migrated)["ticket_id"] == ticket_id
    assert _git(tracker, "diff", "--quiet").returncode == 0
    assert _git(tracker, "diff", "--cached", "--quiet").returncode == 0


def test_migration_collision_aborts_without_partial_commit(tmp_path: Path) -> None:
    from rebar._store.ticket_layout import migrate_flat_ticket_dirs_unit

    tracker = tmp_path / "tracker"
    _init_tracker(tracker)
    ticket_id = _ticket_id(7)
    _write_create(tracker / ticket_id, ticket_id, "flat")
    _write_create(tracker / _shard(ticket_id) / ticket_id, ticket_id, "sharded")
    head = _commit_all(tracker, "colliding seed")

    with pytest.raises(RuntimeError, match="collision"):
        migrate_flat_ticket_dirs_unit(str(tracker))

    assert _git(tracker, "rev-parse", "HEAD").stdout.strip() == head
    assert (tracker / ticket_id).is_dir()
    assert (tracker / _shard(ticket_id) / ticket_id).is_dir()
