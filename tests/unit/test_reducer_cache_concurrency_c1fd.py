"""Held-out reducer-cache atomicity regressions for likeminded-flameproof-tick.

The cache is a rebuildable optimization: a current-hash hit must equal deterministic
event replay, even when real reducer writers overlap.  The synchronization below is
at the cache publication seam; it uses no timing sleeps.
"""

from __future__ import annotations

import builtins
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from rebar._store import fsutil
from rebar.reducer import _api as api_mod
from rebar.reducer import _cache as cache_mod
from rebar.reducer import reduce_ticket

pytestmark = pytest.mark.unit


def _write_event(
    ticket_dir: Path,
    *,
    timestamp: int,
    uuid: str,
    event_type: str,
    data: dict[str, Any],
) -> Path:
    payload = {
        "timestamp": timestamp,
        "uuid": uuid,
        "event_type": event_type,
        "env_id": "00000000-0000-4000-8000-000000000001",
        "author": "Cache Tester",
        "data": data,
    }
    path = ticket_dir / f"{timestamp}-{uuid}-{event_type}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _ticket(tmp_path: Path, name: str = "cache-ticket") -> Path:
    ticket_dir = tmp_path / name
    ticket_dir.mkdir()
    _write_event(
        ticket_dir,
        timestamp=100,
        uuid="11111111-1111-4111-8111-111111111111",
        event_type="CREATE",
        data={"ticket_type": "task", "title": "old-title", "parent_id": None},
    )
    return ticket_dir


def _append_title_edit(ticket_dir: Path) -> None:
    _write_event(
        ticket_dir,
        timestamp=200,
        uuid="22222222-2222-4222-8222-222222222222",
        event_type="EDIT",
        data={"fields": {"title": "new-title"}},
    )


def _replay(ticket_dir: Path) -> dict[str, Any]:
    state = reduce_ticket(ticket_dir, include_retired=True)
    assert state is not None
    return state


def _event_bytes(ticket_dir: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in ticket_dir.glob("*.json")
        if not path.name.startswith(".")
    }


class _BarrierFile:
    """Make every immutable writer hold the same fixed temp inode before writing."""

    def __init__(self, wrapped: Any, barrier: threading.Barrier) -> None:
        self._wrapped = wrapped
        self._barrier = barrier

    def __enter__(self) -> _BarrierFile:
        self._barrier.wait(timeout=10)
        return self

    def __exit__(self, *args: Any) -> Any:
        return self._wrapped.__exit__(*args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


class _DivergentPublication:
    def __init__(self, cache_path: Path) -> None:
        self.cache_path = cache_path
        self.local = threading.local()
        self.old_at_write = threading.Event()
        self.release_old = threading.Event()
        self.new_at_write = threading.Event()
        self.new_renamed = threading.Event()
        self.inodes: list[tuple[int, int]] = []
        self.lock = threading.Lock()
        self.open_barrier = threading.Barrier(2, action=self._assert_shared_inode)

    def _assert_shared_inode(self) -> None:
        assert len(set(self.inodes)) == 1, (
            "precondition: overlapping fixed-temp writers must share one inode"
        )


class _DeferredGenerationFile:
    """Drive the proven open-fd-after-rename interleaving deterministically."""

    def __init__(self, wrapped: Any, control: _DivergentPublication, generation: str) -> None:
        self._wrapped = wrapped
        self._control = control
        self._generation = generation
        self._parts: list[str] = []

    def __enter__(self) -> _DeferredGenerationFile:
        stat = os.fstat(self._wrapped.fileno())
        with self._control.lock:
            self._control.inodes.append((stat.st_dev, stat.st_ino))
        self._control.open_barrier.wait(timeout=10)
        return self

    def write(self, text: str) -> int:
        self._parts.append(text)
        return len(text)

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        if exc_type is not None:
            self._wrapped.close()
            return False

        payload = "".join(self._parts)
        if self._generation == "new":
            self._wrapped.seek(0)
            self._wrapped.write(payload)
            self._wrapped.truncate()
        else:
            assert self._control.new_renamed.wait(timeout=10), (
                "precondition: current generation must publish before the old fd resumes"
            )
            state_offset = payload.index('"state": ') + len('"state": ')
            self._wrapped.seek(state_offset)
            self._wrapped.write(payload[state_offset:])
            self._wrapped.truncate()
        self._wrapped.flush()
        self._wrapped.close()
        return False


def test_current_hash_cache_hit_equals_replay_under_divergent_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket_dir = _ticket(tmp_path)
    cache_path = ticket_dir / ".cache.json"
    control = _DivergentPublication(cache_path)
    real_open = builtins.open
    real_write_cache = api_mod.write_cache
    real_rename = cache_mod.os.rename

    def synchronized_write_cache(*args: Any, **kwargs: Any) -> None:
        generation = getattr(control.local, "generation", None)
        if generation is None:
            real_write_cache(*args, **kwargs)
            return
        if generation == "old":
            control.old_at_write.set()
            assert control.release_old.wait(timeout=10)
        else:
            control.new_at_write.set()
        real_write_cache(*args, **kwargs)

    def controlled_open(path: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        wrapped = real_open(path, mode, *args, **kwargs)
        if os.fspath(path) == os.fspath(cache_path) + ".tmp" and "w" in mode:
            return _DeferredGenerationFile(wrapped, control, control.local.generation)
        return wrapped

    def controlled_rename(src: Any, dst: Any) -> None:
        real_rename(src, dst)
        if os.fspath(dst) == os.fspath(cache_path) and control.local.generation == "new":
            control.new_renamed.set()

    monkeypatch.setattr(api_mod, "write_cache", synchronized_write_cache)
    monkeypatch.setattr(cache_mod, "open", controlled_open, raising=False)
    monkeypatch.setattr(cache_mod.os, "rename", controlled_rename)

    def reduce_generation(generation: str) -> dict[str, Any] | None:
        control.local.generation = generation
        return reduce_ticket(ticket_dir)

    with ThreadPoolExecutor(max_workers=2) as pool:
        old_future = pool.submit(reduce_generation, "old")
        assert control.old_at_write.wait(timeout=10)
        _append_title_edit(ticket_dir)
        event_bytes = _event_bytes(ticket_dir)
        new_future = pool.submit(reduce_generation, "new")
        assert control.new_at_write.wait(timeout=10)
        control.release_old.set()
        old_state = old_future.result(timeout=10)
        new_state = new_future.result(timeout=10)

    assert old_state is not None and old_state["title"] == "old-title"
    assert new_state is not None and new_state["title"] == "new-title"
    if control.inodes:
        assert len(set(control.inodes)) == 1
    assert _event_bytes(ticket_dir) == event_bytes

    replayed = _replay(ticket_dir)
    recovered = reduce_ticket(ticket_dir)
    assert recovered == replayed, (
        "the first post-overlap read must recover the deterministic replay"
    )

    envelope = json.loads(cache_path.read_text(encoding="utf-8"))
    _, current_hash, _, _ = cache_mod.prepare_event_files(str(ticket_dir))
    assert envelope["dir_hash"] == current_hash, (
        "post-recovery cache publication must carry the current event-directory hash"
    )
    cached = reduce_ticket(ticket_dir)
    assert cached == replayed, (
        "a proven current-hash reducer cache hit must equal deterministic replay"
    )
    assert cached["title"] == "new-title"

    cache_path.unlink()
    assert reduce_ticket(ticket_dir) == replayed, "a clean retry must rebuild a coherent cache"
    assert not list(ticket_dir.glob("..cache.json.*.tmp"))
    assert not (ticket_dir / ".cache.json.tmp").exists()


def test_eight_identical_reducers_emit_no_cache_rename_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ticket_dir = _ticket(tmp_path, "immutable-ticket")
    event_bytes = _event_bytes(ticket_dir)
    barrier = threading.Barrier(8)
    real_open = builtins.open

    def controlled_open(path: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        wrapped = real_open(path, mode, *args, **kwargs)
        if os.fspath(path) == os.fspath(ticket_dir / ".cache.json.tmp") and "w" in mode:
            return _BarrierFile(wrapped, barrier)
        return wrapped

    monkeypatch.setattr(cache_mod, "open", controlled_open, raising=False)
    with caplog.at_level(logging.WARNING, logger="rebar.reducer._cache"):
        with ThreadPoolExecutor(max_workers=8) as pool:
            states = list(pool.map(lambda _: reduce_ticket(ticket_dir), range(8)))

    replayed = _replay(ticket_dir)
    assert all(state == replayed for state in states)
    assert _event_bytes(ticket_dir) == event_bytes
    file_not_found = [
        record for record in caplog.records if isinstance(record.exc_info[1], FileNotFoundError)
    ]
    assert not file_not_found, "identical reducer writers must not race a shared temp rename"
    assert not list(ticket_dir.glob("..cache.json.*.tmp"))
    assert not (ticket_dir / ".cache.json.tmp").exists()


def test_serialized_generations_publish_coherent_cache(tmp_path: Path) -> None:
    ticket_dir = _ticket(tmp_path, "serialized-ticket")
    assert reduce_ticket(ticket_dir)["title"] == "old-title"
    _append_title_edit(ticket_dir)
    assert reduce_ticket(ticket_dir) == _replay(ticket_dir)


def test_corrupt_create_uses_canonical_cache_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket_dir = tmp_path / "corrupt-create"
    ticket_dir.mkdir()
    _write_event(
        ticket_dir,
        timestamp=100,
        uuid="33333333-3333-4333-8333-333333333333",
        event_type="CREATE",
        data={"title": "missing ticket type", "parent_id": None},
    )
    write_calls: list[tuple[str, str, dict[str, Any], str]] = []
    real_write_cache = cache_mod.write_cache

    def spy_write_cache(
        cache_path: str, dir_hash: str, state: dict[str, Any], ticket_dir: str
    ) -> None:
        write_calls.append((cache_path, dir_hash, state, ticket_dir))
        real_write_cache(cache_path, dir_hash, state, ticket_dir)

    monkeypatch.setattr(cache_mod, "write_cache", spy_write_cache)
    state = reduce_ticket(ticket_dir)

    assert state is not None and state["status"] == "fsck_needed"
    envelope = json.loads((ticket_dir / ".cache.json").read_text(encoding="utf-8"))
    assert write_calls == [
        (
            str(ticket_dir / ".cache.json"),
            envelope["dir_hash"],
            state,
            str(ticket_dir),
        )
    ], "corrupt-CREATE replay must delegate exactly once to canonical cache publication"
    assert envelope == {"dir_hash": write_calls[0][1], "state": state}
    assert not list(ticket_dir.glob("..cache.json.*.tmp"))
    assert not (ticket_dir / ".cache.json.tmp").exists()


def test_cache_publication_failure_is_best_effort_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ticket_dir = _ticket(tmp_path, "failed-publication")

    def fail_publish(*args: Any, **kwargs: Any) -> None:
        raise OSError("injected cache publication failure")

    monkeypatch.setattr(cache_mod.os, "rename", fail_publish)
    monkeypatch.setattr(fsutil.os, "replace", fail_publish)
    with caplog.at_level(logging.WARNING, logger="rebar.reducer._cache"):
        state = reduce_ticket(ticket_dir)

    assert state == _replay(ticket_dir), "cache failure must not change replayed state"
    assert "failed to write cache" in caplog.text
    assert not list(ticket_dir.glob("..cache.json.*.tmp")), (
        "failed publication must remove every private cache temp artifact"
    )
    assert not (ticket_dir / ".cache.json.tmp").exists()
