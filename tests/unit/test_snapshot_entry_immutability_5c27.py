"""Bug 5c27-7926: snapshot-store entries must be immutable — reads must not write into them.

``<store>/tickets-<sha>/`` is documented as an immutable, content-addressed entry
(ADR 0005 D2), but every ticket read through a pinned root wrote a derived reducer cache
(``.cache.json``) INSIDE the entry (``reducer/_api.py`` via ``write_cache``, plus the
corrupt-CREATE replay path in ``reducer/_processors.py``). Measured in the wild: 4,844
``.cache.json`` files inside one live entry, all created by reads after materialization.

Why it matters: (1) bug 8386's fix hardlinks unchanged blobs between adjacent entries, so
an in-place write inside an entry would corrupt every entry sharing the inode — today that
is prevented only by ``atomic_write`` publishing via ``os.replace``; (2) the janitor's
reverify pass records a TOFU digest over the entry's contents, so post-read writes make a
clean entry look corrupt and evict it; (3) it inflates the store.

The fix keeps the reducer cache OUT of snapshot entries (the cache write is disabled when
the ticket dir lies inside a store entry — the ticket's sanctioned least-behavioral option)
and pins the rename-over contract in ``_store/fsutil.py`` so a future in-place write cannot
silently corrupt shared inodes.

Everything here is offline: no network, no LLM.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from rebar._snapshot import repo_snapshot as rs
from rebar._store.fsutil import atomic_write
from rebar.reducer import reduce_ticket
from rebar.reducer._cache import write_cache


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--quiet")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    return path


@pytest.fixture(autouse=True)
def _isolate_store(monkeypatch, tmp_path):
    store = tmp_path / "gate-tmpdir"
    store.mkdir()
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(store))


_TICKET_DIR = "11111111-2222-4333-8444-555555555555"


def _tickets_repo_with_event(tmp_path: Path) -> Path:
    """A repo whose ``tickets`` branch tree mimics the tracker layout: one ticket dir
    holding one valid CREATE event."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "seed.txt").write_text("seed")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "seed")
    _git(repo, "checkout", "--quiet", "-b", "tickets")
    tdir = repo / _TICKET_DIR
    tdir.mkdir()
    event = {
        "timestamp": 100,
        "uuid": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        "event_type": "CREATE",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "author": "Immutability Tester",
        "data": {"ticket_type": "task", "title": "immutable-entry", "parent_id": None},
    }
    (tdir / "100-aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee-CREATE.json").write_text(json.dumps(event))
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "tickets")
    _git(repo, "checkout", "--quiet", "-")
    return repo


def _entry_files(entry: Path) -> set[str]:
    return {str(p.relative_to(entry)) for p in entry.rglob("*") if p.is_file()}


def test_reading_a_ticket_does_not_write_into_the_snapshot_entry(tmp_path):
    """AC1 + AC2: a materialized entry's file set is unchanged by a read through the
    pinned root — the reducer must not publish its derived cache inside the entry."""
    repo = _tickets_repo_with_event(tmp_path)
    entry = Path(rs.materialize_tickets("tickets", repo_root=str(repo), fetch=False))
    before = _entry_files(entry)

    result = reduce_ticket(entry / ".tickets-tracker" / _TICKET_DIR)

    assert result is not None and result["title"] == "immutable-entry", (
        "the read itself must still work"
    )
    after = _entry_files(entry)
    assert after == before, (
        f"a read must not write into the immutable entry; new files: {sorted(after - before)}"
    )


def test_reducer_cache_still_written_outside_the_store(tmp_path):
    """The guard must be scoped to snapshot entries: an ordinary tracker checkout keeps
    its cache (the cache is a real optimization there)."""
    ticket_dir = tmp_path / "tracker" / _TICKET_DIR
    ticket_dir.mkdir(parents=True)
    cache_path = ticket_dir / ".cache.json"
    write_cache(str(cache_path), "hash", {"ok": True}, str(ticket_dir))
    assert cache_path.is_file(), "outside the store the cache write must be unchanged"


def test_in_snapshot_entry_predicate(tmp_path):
    """The detector is structural (the store layout in the path itself), so it holds in
    any process regardless of how the pinned root was handed to it."""
    sha = "00b00a6ee3e2b6c0ebb667c044eb303ccf2ba3e3"
    store = tmp_path / "rebar-gate-snapshots"
    inside_tickets = store / f"tickets-{sha}" / ".tickets-tracker" / _TICKET_DIR
    inside_code = store / sha / "src"
    assert rs.in_snapshot_entry(inside_tickets)
    assert rs.in_snapshot_entry(inside_code)
    # Not an entry: an ordinary tracker, and a store-named dir without an entry-shaped child.
    assert not rs.in_snapshot_entry(tmp_path / "repo" / ".tickets-tracker" / _TICKET_DIR)
    assert not rs.in_snapshot_entry(store / "tmp" / "build-123")
    assert not rs.in_snapshot_entry(store / "tickets-notahexsha" / "x")


def test_atomic_write_publishes_by_rename_never_in_place(tmp_path):
    """AC3: the rename-over contract. Hardlink blob-sharing between entries (bug 8386) is
    safe only because ``atomic_write`` publishes a NEW inode via ``os.replace`` — an
    in-place write through a shared inode would silently corrupt every entry linking it.
    This pins the contract behaviorally: the sibling link still holds the old bytes."""
    target = tmp_path / "blob"
    target.write_text("published-generation")
    sibling = tmp_path / "shared-into-another-entry"
    os.link(target, sibling)

    atomic_write(str(target), "next-generation")

    assert target.read_text() == "next-generation"
    assert sibling.read_text() == "published-generation", (
        "atomic_write wrote IN PLACE through a shared inode — the rename-over contract broke"
    )
    assert os.lstat(target).st_ino != os.lstat(sibling).st_ino
