from __future__ import annotations

import pytest

from rebar._snapshot import repo_snapshot


def test_materialize_refuses_before_clone_when_snapshot_volume_below_floor(monkeypatch, tmp_path):
    sha = "a" * 40
    store = tmp_path / "store"
    store.mkdir()

    monkeypatch.setattr(repo_snapshot, "resolve_ref", lambda *args, **kwargs: sha)
    monkeypatch.setattr(repo_snapshot, "store_root", lambda: store)
    monkeypatch.setattr(
        repo_snapshot, "_snapshot_store_has_room", lambda _root: False, raising=False
    )

    def fail_materialize(*args, **kwargs):
        raise AssertionError("materialization must not start below the hard free-space floor")

    monkeypatch.setattr(repo_snapshot, "_materialize_tree", fail_materialize)

    with pytest.raises(Exception) as exc_info:
        repo_snapshot.materialize("origin/main", repo_root=str(tmp_path), fetch=False)

    assert type(exc_info.value).__name__ == "SnapshotLowDiskError"
