"""Reduction cache: hit / miss / invalidation + warm-cache performance

Split from the former monolithic tests/scripts/test_ticket_reducer.py along
reducer-concern seams. The module-under-test fixture (`reducer`) lives in
conftest.py; event-writing helpers (`_write_event`, `_UUID*`) in _events.py.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import ModuleType

import pytest
from _events import _UUID, _UUID2, _UUID3, _write_event

# ---------------------------------------------------------------------------
# Test 12: Cache hit — second call with no file changes returns cached state
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_cache_hit_returns_cached_state(tmp_path: Path, reducer: ModuleType) -> None:
    """Calling reduce_ticket twice with no file changes must serve from cache.

    Without the fix: ticket-reducer.py does not yet implement caching. The assert on
    .cache.json existing will fail because the current implementation never
    writes a cache file.

    Setup: write a CREATE event, call reduce_ticket() once (expected to warm
    the cache and write .cache.json), then call reduce_ticket() again without
    modifying any files.

    Asserts:
      - .cache.json exists in the ticket directory after the first call
      - Second call returns the same state as first (cache hit — same dir_hash)
    """
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


# ---------------------------------------------------------------------------
# Test 13: Cache miss on directory listing change (file addition)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_cache_miss_on_directory_listing_change(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Adding an event file between calls must invalidate the cache.

    Without the fix: without caching, the test structure is valid but the cache-miss
    detection mechanism doesn't exist. Once caching is implemented, a new
    file changes the dir_hash → cache miss → recompute.

    Setup: write a CREATE event, call reduce_ticket() (warms cache), write a
    STATUS event, call reduce_ticket() again.

    Asserts:
      - Second call returns updated state reflecting the STATUS event
      - .cache.json exists (written after first call — after first call)
    """
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


# ---------------------------------------------------------------------------
# Test 14: Cache invalidated on file deletion
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_cache_invalidated_on_file_deletion(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Deleting an event file between calls must invalidate the cache.

    Without the fix: without caching, the second call already sees 0 comments because
    the file is gone. However, the assertion that .cache.json is UPDATED
    after the recompute will fail since no cache file is ever written.

    This is critical for w21-q0nn compaction: cache must detect file
    DELETIONS, not just additions.

    Setup: write CREATE + STATUS + COMMENT events, call reduce_ticket()
    (warm cache), delete the COMMENT file, call reduce_ticket() again.

    Asserts:
      - Second call returns state with 0 comments (deletion detected, recomputed)
      - .cache.json exists after first call
      - .cache.json is updated after second call (recompute after cache miss)
    """
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
    assert cache_file.exists(), (
        ".cache.json must still exist after recompute following deletion"
    )
    mtime_after_recompute = cache_file.stat().st_mtime
    assert mtime_after_recompute != mtime_after_warm, (
        ".cache.json must be updated (mtime changed) after cache-miss recompute "
        "triggered by file deletion"
    )


# ---------------------------------------------------------------------------
# Test 15: Warm cache 200 tickets under 500ms
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
@pytest.mark.benchmark
@pytest.mark.skipif(
    os.environ.get("CI") == "true",
    reason="Wall-clock benchmark skipped on CI runners (use @pytest.mark.benchmark exclusion)",
)
def test_warm_cache_200_tickets_under_500ms(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """200 warm-cache reduce_ticket() calls must complete in under 500ms.

    Setup: create 200 ticket directories each with a CREATE event, warm the
    cache by calling reduce_ticket() on each (first pass), then time the
    second pass (all cache hits).

    Marked @pytest.mark.benchmark so this test can be excluded from standard
    unit runs on constrained CI runners: pytest -m "not benchmark".
    """
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


# ---------------------------------------------------------------------------
# Test 16: Warm cache 1000 tickets under 2s
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
@pytest.mark.benchmark
@pytest.mark.skipif(
    os.environ.get("CI") == "true",
    reason="Wall-clock benchmark skipped on CI runners (use @pytest.mark.benchmark exclusion)",
)
def test_warm_cache_1000_tickets_under_2s(tmp_path: Path, reducer: ModuleType) -> None:
    """1000 warm-cache reduce_ticket() calls must complete in under 2 seconds.

    Setup: create 1000 ticket directories each with a CREATE event, warm the
    cache by calling reduce_ticket() on each (first pass), then time the
    second pass (all cache hits).

    Marked @pytest.mark.benchmark so this test can be excluded from standard
    unit runs on constrained CI runners: pytest -m "not benchmark".
    """
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


# ---------------------------------------------------------------------------
# Test 17: Cache miss on same-filename content change (file overwrite)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_cache_miss_on_same_filename_content_change(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """Overwriting an event file with different content (same filename) must invalidate cache.

    This test guards against the filename-only hash bug: if the cache hash
    covers only filenames and not file sizes, an in-place overwrite of an
    event file will silently return stale state.

    Setup: write a CREATE event with title "Original title", call
    reduce_ticket() (warms cache), then overwrite the same CREATE event
    file with a different title. Call reduce_ticket() again.

    Asserts:
      - First call returns the original title.
      - Second call (after overwrite) returns the updated title — cache miss.
    """
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
    assert state1["title"] == "Original title", (
        "Setup: first call must return the original title"
    )

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


# ---------------------------------------------------------------------------
# Test 17b: Cache miss on same-SIZE in-place content rewrite (bug 1d76-b6d1)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_cache_miss_on_same_size_inplace_rewrite(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """A same-byte-length in-place rewrite of an event file must invalidate cache.

    Regression guard for bug 1d76-b6d1: the dir-hash keyed on filename+size only
    cannot detect an equal-length overwrite (as produced by a git checkout/rebase
    of the tickets branch or an fsck-recover cherry-pick), so reads served stale
    state. The fix folds st_mtime_ns into the hash.

    Setup: write a CREATE event with a title, warm the cache, then overwrite the
    same file in place with a DIFFERENT title of the SAME byte length and bump
    its mtime (as a checkout would). The next read must reflect the new title.
    Also asserts the cache still HITS on an unchanged dir (no read-path
    regression).
    """
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
