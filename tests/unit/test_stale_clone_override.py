"""Stale-clone forced override on reconnect after a reclaim publish-swap (ADR 0106 §5).

Every repository here is a throwaway ``git init`` under ``tmp_path``. The "remote" is
rewritten into a parentless baseline carrying a superseding store epoch, which is what a
reclaim publish-swap publishes; the clone then reconverges through
:func:`rebar._store.sync._do_reconverge` under test.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rebar._store import compat, stale_override, sync

pytestmark = pytest.mark.unit

NOW = datetime.now(UTC)
TICKET = "aaaa-1111-2222-3333"
OLD_EPOCH = "2026-10-01T00-00-00Z-0001"
NEW_EPOCH = "2026-11-10T00-00-00Z-0002"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def _ok(repo: Path, *args: str) -> str:
    proc = _git(repo, *args)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _uuid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-{n:012x}"


def _ns(moment: datetime) -> int:
    return int(moment.timestamp() * 1_000_000_000)


def _event_name(n: int, when: datetime, kind: str = "COMMENT") -> str:
    return f"{_ns(when)}-{_uuid(n)}-{kind}.json"


def _write(repo: Path, rel: str, body: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _record(epoch: str | None, lineage: object = ...) -> str:
    body: dict[str, object] = {
        "format_version": compat.CURRENT_FORMAT_VERSION,
        "required_capabilities": [],
    }
    if epoch is not None:
        body["epoch"] = epoch
    if lineage is not ...:
        body["epoch_lineage"] = lineage
    return json.dumps(body, indent=2, sort_keys=True) + "\n"


def _commit(repo: Path, message: str) -> str:
    _ok(repo, "add", "-A")
    _ok(repo, "commit", "-q", "--no-verify", "-m", message)
    return _ok(repo, "rev-parse", "HEAD")


def _init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _ok(path, "init", "-q", "-b", "tickets")
    _ok(path, "config", "user.email", "t@example.com")
    _ok(path, "config", "user.name", "T")
    return path


def _shared_store(tmp_path: Path) -> tuple[Path, Path]:
    """A remote store at OLD_EPOCH with two events, plus a converged clone of it."""
    origin = _init(tmp_path / "origin")
    _write(origin, compat.COMPAT_FILENAME, _record(OLD_EPOCH))
    _write(origin, f"{TICKET}/{_event_name(1, NOW - timedelta(days=90), 'CREATE')}", '{"n":1}\n')
    _write(origin, f"{TICKET}/{_event_name(2, NOW - timedelta(days=80))}", '{"n":2}\n')
    _commit(origin, "base")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", "-b", "tickets", str(origin), str(clone)],
        check=True,
        capture_output=True,
    )
    _ok(clone, "config", "user.email", "t@example.com")
    _ok(clone, "config", "user.name", "T")
    return origin, clone


def _reclaim_remote(origin: Path, *, record: str | None = None, extra: dict | None = None) -> str:
    """Replace the remote's history with a parentless baseline of its current tree."""
    _ok(origin, "checkout", "-q", "--orphan", "rewritten")
    _write(origin, compat.COMPAT_FILENAME, record or _record(NEW_EPOCH, [OLD_EPOCH]))
    for rel, body in (extra or {}).items():
        _write(origin, rel, body)
    sha = _commit(origin, "reclaimed baseline")
    _ok(origin, "branch", "-M", "rewritten", "tickets")
    return sha


def _reconverge(clone: Path, monkeypatch: pytest.MonkeyPatch, horizon_days: int = 30) -> None:
    monkeypatch.setattr(sync, "_reclaim_horizon_days", lambda _tracker: horizon_days)
    _ok(clone, "fetch", "-q", "origin", "+refs/heads/tickets:refs/remotes/origin/tickets")
    sync._do_reconverge(str(clone), "tickets", "origin")


def _local_event(clone: Path, n: int, when: datetime, body: str = '{"local":true}\n') -> str:
    rel = f"{TICKET}/{_event_name(n, when)}"
    _write(clone, rel, body)
    _commit(clone, f"local event {n}")
    return rel


def _backup_refs(clone: Path) -> list[str]:
    out = _ok(clone, "for-each-ref", "--format=%(refname) %(objectname)", "refs/rebar/")
    return [line for line in out.splitlines() if line]


def test_stale_clone_is_overridden_not_merged(tmp_path, monkeypatch) -> None:
    origin, clone = _shared_store(tmp_path)
    stale_rel = _local_event(clone, 3, NOW - timedelta(days=45))
    old_head = _ok(clone, "rev-parse", "HEAD")
    remote_sha = _reclaim_remote(origin)

    _reconverge(clone, monkeypatch)

    assert _ok(clone, "rev-parse", "HEAD") == remote_sha
    assert not (clone / stale_rel).exists()
    assert _git(clone, "merge-base", "HEAD", old_head).returncode != 0
    refs = _backup_refs(clone)
    assert len(refs) == 1
    ref, sha = refs[0].split()
    assert ref.startswith(stale_override.BACKUP_REF_PREFIX)
    assert sha == old_head


def test_fresh_clone_replays_unpushed_events_on_top_of_remote(tmp_path, monkeypatch) -> None:
    origin, clone = _shared_store(tmp_path)
    body = '{"fresh": "bytes stay identical"}\n'
    fresh_rel = _local_event(clone, 3, NOW - timedelta(days=2), body)
    remote_sha = _reclaim_remote(origin)

    _reconverge(clone, monkeypatch)

    assert _ok(clone, "rev-parse", "HEAD^") == remote_sha
    added = _ok(clone, "diff", "--name-only", f"{remote_sha}..HEAD").splitlines()
    assert added == [fresh_rel]
    assert (clone / fresh_rel).read_text(encoding="utf-8") == body
    assert _ok(clone, "status", "--porcelain") == ""


def test_retired_or_folded_events_are_not_resurrected(tmp_path, monkeypatch) -> None:
    origin, clone = _shared_store(tmp_path)
    retired_rel = _local_event(clone, 4, NOW - timedelta(days=3))
    folded_rel = _local_event(clone, 5, NOW - timedelta(days=3))
    fresh_rel = _local_event(clone, 6, NOW - timedelta(days=1))
    snapshot = {"data": {"source_event_uuids": [_uuid(5)]}}
    remote_sha = _reclaim_remote(
        origin,
        extra={
            retired_rel + ".retired": '{"local":true}\n',
            f"{TICKET}/{_event_name(9, NOW - timedelta(days=2), 'SNAPSHOT')}": json.dumps(snapshot),
        },
    )

    _reconverge(clone, monkeypatch)

    added = _ok(clone, "diff", "--name-only", f"{remote_sha}..HEAD").splitlines()
    assert added == [fresh_rel]
    assert not (clone / retired_rel).exists()
    assert not (clone / folded_rel).exists()


def test_clone_without_unpushed_events_resets_to_remote(tmp_path, monkeypatch) -> None:
    origin, clone = _shared_store(tmp_path)
    remote_sha = _reclaim_remote(origin)

    _reconverge(clone, monkeypatch)

    assert _ok(clone, "rev-parse", "HEAD") == remote_sha
    assert len(_backup_refs(clone)) == 1


@pytest.mark.parametrize(
    "record",
    [
        _record("2026-11-10T00-00-00Z-ffff", ["some-other-epoch"]),
        _record(NEW_EPOCH, "not-a-list"),
        _record(NEW_EPOCH),
    ],
    ids=["unrelated-lineage", "malformed-lineage", "no-lineage"],
)
def test_unproven_rewrites_keep_the_diverged_refusal(tmp_path, monkeypatch, caplog, record) -> None:
    origin, clone = _shared_store(tmp_path)
    _local_event(clone, 3, NOW - timedelta(days=45))
    old_head = _ok(clone, "rev-parse", "HEAD")
    _reclaim_remote(origin, record=record)

    with caplog.at_level(logging.WARNING, logger=sync.logger.name):
        _reconverge(clone, monkeypatch)

    assert _ok(clone, "rev-parse", "HEAD") == old_head
    assert _backup_refs(clone) == []
    assert "DIVERGED" in caplog.text


def test_write_compat_record_preserves_epoch_lineage(tmp_path) -> None:
    tracker = tmp_path / "t"
    tracker.mkdir()
    _write(tracker, compat.COMPAT_FILENAME, _record(NEW_EPOCH, [None, OLD_EPOCH]))

    compat.write_compat_record(tracker)

    record = json.loads((tracker / compat.COMPAT_FILENAME).read_text(encoding="utf-8"))
    assert record["epoch"] == NEW_EPOCH
    assert record["epoch_lineage"] == [None, OLD_EPOCH]


def test_superseding_record_appends_previous_epoch() -> None:
    legacy = json.loads(_record(None))
    first = compat.superseding_record(legacy, OLD_EPOCH)
    second = compat.superseding_record(first, NEW_EPOCH)

    assert first["epoch_lineage"] == [None]
    assert second == {**legacy, "epoch": NEW_EPOCH, "epoch_lineage": [None, OLD_EPOCH]}
    with pytest.raises(ValueError):
        compat.superseding_record(second, OLD_EPOCH)
