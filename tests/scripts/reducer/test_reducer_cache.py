"""Exercise reduction-cache hits, invalidation dimensions, and warm-read bounds."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import ModuleType

import pytest
from _events import _UUID, _UUID2, _UUID3, _write_event

# Test 12: unchanged input hits the cache


@pytest.mark.unit
@pytest.mark.scripts
def test_cache_hit_returns_cached_state(tmp_path: Path, reducer: ModuleType) -> None:
    """An unchanged directory returns identical state from the written cache."""
    ticket_dir = tmp_path / "tkt-cache-hit"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={
            "ticket_type": "task",
            "title": "Cache hit test",
            "parent_id": None,
        },
        author="Alice",
    )

    # First call — expected to warm cache and write .cache.json
    state1 = reducer.reduce_ticket(ticket_dir)

    # Cache file must exist after first call
    cache_file = ticket_dir / ".cache.json"
    assert cache_file.exists(), (
        ".cache.json must be written by reduce_ticket() after first call; "
        "caching is not yet implemented"
    )

    # Second call — no files changed; must return same state (cache hit)
    state2 = reducer.reduce_ticket(ticket_dir)

    assert state1 is not None
    assert state2 is not None
    assert state1 == state2, (
        "Second call with no file changes must return identical state (cache hit)"
    )


# Test 13: adding an event invalidates the cache


@pytest.mark.unit
@pytest.mark.scripts
def test_cache_miss_on_directory_listing_change(tmp_path: Path, reducer: ModuleType) -> None:
    """Adding an event invalidates the cache and returns the updated state."""
    ticket_dir = tmp_path / "tkt-cache-miss"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={
            "ticket_type": "task",
            "title": "Cache miss test",
            "parent_id": None,
        },
        author="Alice",
    )

    # First call — warms cache
    state1 = reducer.reduce_ticket(ticket_dir)

    # Cache file must exist after first call
    cache_file = ticket_dir / ".cache.json"
    assert cache_file.exists(), (
        ".cache.json must be written by reduce_ticket() after first call; "
        "caching is not yet implemented"
    )

    # Add a STATUS event — changes directory listing → cache miss
    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_UUID2,
        event_type="STATUS",
        data={"status": "in_progress", "current_status": "open"},
    )

    # Second call — new file detected; cache invalidated → recompute
    state2 = reducer.reduce_ticket(ticket_dir)

    assert state1 is not None
    assert state2 is not None
    assert state2["status"] == "in_progress", (
        "After adding a STATUS event, reduce_ticket() must recompute state "
        "and return updated status (cache miss detected)"
    )


# Test 14: deletion invalidates cache


@pytest.mark.unit
@pytest.mark.scripts
def test_cache_invalidated_on_file_deletion(tmp_path: Path, reducer: ModuleType) -> None:
    """Deleting an event invalidates and rewrites the cache with current state."""
    ticket_dir = tmp_path / "tkt-cache-delete"
    ticket_dir.mkdir()

    _write_event(
        ticket_dir,
        timestamp=1742605200,
        uuid=_UUID,
        event_type="CREATE",
        data={
            "ticket_type": "task",
            "title": "Cache deletion test",
            "parent_id": None,
        },
        author="Alice",
    )

    _write_event(
        ticket_dir,
        timestamp=1742605300,
        uuid=_UUID2,
        event_type="STATUS",
        data={"status": "in_progress", "current_status": "open"},
    )

    comment_file = _write_event(
        ticket_dir,
        timestamp=1742605400,
        uuid=_UUID3,
        event_type="COMMENT",
        data={"body": "a comment that will be deleted"},
        author="Bob",
    )

    # First call — warm cache; state has 1 comment
    state1 = reducer.reduce_ticket(ticket_dir)
    assert state1 is not None
    assert len(state1["comments"]) == 1, "Setup: first call must see the COMMENT event"

    # Cache file must exist after first call
    cache_file = ticket_dir / ".cache.json"
    assert cache_file.exists(), (
        ".cache.json must be written by reduce_ticket() after first call; "
        "caching is not yet implemented"
    )

    # Capture mtime of cache file before deletion-triggered recompute
    mtime_after_warm = cache_file.stat().st_mtime if cache_file.exists() else None

    # Delete the COMMENT file — changes directory listing → cache miss
    comment_file.unlink()

    # Second call — deletion detected; cache invalidated → recompute
    state2 = reducer.reduce_ticket(ticket_dir)

    assert state2 is not None
    assert len(state2["comments"]) == 0, (
        "After deleting the COMMENT event file, reduce_ticket() must recompute "
        "state and return 0 comments (cache invalidated on file deletion)"
    )

    # Cache file must be updated after recompute (mtime must change)
    assert cache_file.exists(), ".cache.json must still exist after recompute following deletion"
    mtime_after_recompute = cache_file.stat().st_mtime
    assert mtime_after_recompute != mtime_after_warm, (
        ".cache.json must be updated (mtime changed) after cache-miss recompute "
        "triggered by file deletion"
    )


# Test 15: 200 warm reads


@pytest.mark.unit
@pytest.mark.scripts
@pytest.mark.benchmark
@pytest.mark.skipif(
    os.environ.get("CI") == "true",
    reason="Wall-clock benchmark skipped on CI runners (use @pytest.mark.benchmark exclusion)",
)
def test_warm_cache_200_tickets_under_500ms(tmp_path: Path, reducer: ModuleType) -> None:
    """Two hundred warm reads complete within 500 ms outside CI."""
    ticket_dirs: list[Path] = []
    for i in range(200):
        ticket_dir = tmp_path / f"tkt-{i:04d}"
        ticket_dir.mkdir()
        _write_event(
            ticket_dir,
            timestamp=1742605200 + i,
            uuid=f"00000000-0000-4000-8000-{i:012d}",
            event_type="CREATE",
            data={
                "ticket_type": "task",
                "title": f"Benchmark ticket {i}",
                "parent_id": None,
            },
            author="Bench",
        )
        ticket_dirs.append(ticket_dir)

    # First pass — warm cache (cache miss, OK to be slow)
    for td in ticket_dirs:
        reducer.reduce_ticket(td)

    # Second pass — all cache hits; measure elapsed time
    start = time.monotonic()
    for td in ticket_dirs:
        reducer.reduce_ticket(td)
    elapsed = time.monotonic() - start

    assert elapsed < 0.5, f"200 warm-cache calls took {elapsed:.3f}s, must be < 0.5s"


# Test 16: 1,000 warm reads


@pytest.mark.unit
@pytest.mark.scripts
@pytest.mark.benchmark
@pytest.mark.skipif(
    os.environ.get("CI") == "true",
    reason="Wall-clock benchmark skipped on CI runners (use @pytest.mark.benchmark exclusion)",
)
def test_warm_cache_1000_tickets_under_2s(tmp_path: Path, reducer: ModuleType) -> None:
    """One thousand warm reads complete within two seconds outside CI."""
    ticket_dirs: list[Path] = []
    for i in range(1000):
        ticket_dir = tmp_path / f"tkt-{i:04d}"
        ticket_dir.mkdir()
        _write_event(
            ticket_dir,
            timestamp=1742605200 + i,
            uuid=f"00000000-0000-4000-8000-{i:012d}",
            event_type="CREATE",
            data={
                "ticket_type": "task",
                "title": f"Benchmark ticket {i}",
                "parent_id": None,
            },
            author="Bench",
        )
        ticket_dirs.append(ticket_dir)

    # First pass — warm cache (cache miss, OK to be slow)
    for td in ticket_dirs:
        reducer.reduce_ticket(td)

    # Second pass — all cache hits; measure elapsed time
    start = time.monotonic()
    for td in ticket_dirs:
        reducer.reduce_ticket(td)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"1000 warm-cache calls took {elapsed:.3f}s, must be < 2.0s"


# Test 17: same-name content changes invalidate the cache


@pytest.mark.unit
@pytest.mark.scripts
def test_cache_miss_on_same_filename_content_change(tmp_path: Path, reducer: ModuleType) -> None:
    """An in-place content-and-size change invalidates an unchanged filename."""
    ticket_dir = tmp_path / "tkt-content-change"
    ticket_dir.mkdir()

    create_filename = f"1742605200-{_UUID}-CREATE.json"
    create_path = ticket_dir / create_filename

    # Write original CREATE event
    original_payload = {
        "timestamp": 1742605200,
        "uuid": _UUID,
        "event_type": "CREATE",
        "env_id": "00000000-0000-4000-8000-000000000001",
        "author": "Alice",
        "data": {
            "ticket_type": "task",
            "title": "Original title",
            "parent_id": None,
        },
    }
    create_path.write_text(json.dumps(original_payload))

    # First call — warm cache
    state1 = reducer.reduce_ticket(ticket_dir)
    assert state1 is not None
    assert state1["title"] == "Original title", "Setup: first call must return the original title"

    # Overwrite same file with updated title (same filename, different content and size)
    updated_payload = {
        **original_payload,
        "data": {
            **original_payload["data"],
            "title": "Updated title after content change",
        },
    }
    create_path.write_text(json.dumps(updated_payload))

    # Second call — content changed; cache must be invalidated → recompute
    state2 = reducer.reduce_ticket(ticket_dir)
    assert state2 is not None
    assert state2["title"] == "Updated title after content change", (
        "After overwriting event file content, reduce_ticket() must recompute state "
        "and return the updated title (cache miss on content change); "
        f"got title={state2['title']!r}"
    )


# Test 17b: equal-size rewrites invalidate by modification time


@pytest.mark.unit
@pytest.mark.scripts
def test_cache_miss_on_same_size_inplace_rewrite(tmp_path: Path, reducer: ModuleType) -> None:
    """An equal-size rewrite changes mtime and invalidates without breaking hits."""
    ticket_dir = tmp_path / "tkt-same-size-rewrite"
    ticket_dir.mkdir()

    create_filename = f"1742605200-{_UUID}-CREATE.json"
    create_path = ticket_dir / create_filename

    # Two titles of identical length -> identical JSON byte length on disk.
    title_a = "AAAAAAAAAA"
    title_b = "BBBBBBBBBB"
    assert len(title_a) == len(title_b)

    def _payload(title: str) -> dict:
        return {
            "timestamp": 1742605200,
            "uuid": _UUID,
            "event_type": "CREATE",
            "env_id": "00000000-0000-4000-8000-000000000001",
            "author": "Alice",
            "data": {"ticket_type": "task", "title": title, "parent_id": None},
        }

    blob_a = json.dumps(_payload(title_a))
    blob_b = json.dumps(_payload(title_b))
    assert len(blob_a) == len(blob_b), "Setup: blobs must be equal byte length"

    create_path.write_text(blob_a)

    # First call — warm cache.
    state1 = reducer.reduce_ticket(ticket_dir)
    assert state1 is not None
    assert state1["title"] == title_a, "Setup: first call must return original title"

    cache_file = ticket_dir / ".cache.json"
    assert cache_file.exists(), ".cache.json must be written after first call"

    # No-change second call MUST hit the cache (cache still effective — no
    # regression): the cache file must not be rewritten.
    cache_mtime_before = cache_file.stat().st_mtime_ns
    state_hit = reducer.reduce_ticket(ticket_dir)
    assert state_hit == state1, "Unchanged dir must serve identical cached state"
    assert cache_file.stat().st_mtime_ns == cache_mtime_before, (
        "Unchanged dir must be a cache HIT (cache file must not be rewritten)"
    )

    # In-place same-size overwrite + mtime bump (simulating a git checkout).
    create_path.write_text(blob_b)
    st = create_path.stat()
    os.utime(create_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert create_path.stat().st_size == st.st_size, "rewrite must be same size"

    # Next read must reflect the new content (cache miss on same-size rewrite).
    state2 = reducer.reduce_ticket(ticket_dir)
    assert state2 is not None
    assert state2["title"] == title_b, (
        "After a same-size in-place rewrite, reduce_ticket() must recompute and "
        f"return the updated title (cache miss on equal-length rewrite); "
        f"got title={state2['title']!r}"
    )
