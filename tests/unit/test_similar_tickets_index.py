"""The lexical similar-ticket index: ranking, catch-up, and failure disposition.

Ticket 47c8-97ac-eff3-4e88. These exercise the three properties the design rests on:

* ranking reproduces the weighting ADR 0122 measured, and publishes a score a caller
  can actually assert on;
* catch-up reconciles IN PLACE, so a diff naming one ticket cannot disturb another,
  and its two-level mtime/hash filter never lets the mtime decide;
* every failure path returns an empty result after logging, because retrieval is
  consulted on the create path and an advisory that raised would fail the create.

Stores here are built by writing event files directly rather than through
``rebar.create_ticket``: the reducer folds a plain event directory, so the tests need
no git repository and stay in the unit tier.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import pytest

from rebar import similar
from rebar.similar import find_similar
from rebar.similar.catchup import UNKNOWN_MTIME, _changed, catch_up, dir_hash_of, live_entries
from rebar.similar.index import (
    _FTS_COLUMNS,
    BM25_WEIGHTS,
    INDEX_VERSION,
    index_path,
    open_index,
)
from rebar.similar.lexical import RESULT_LIMIT, match_expression, query_text, search

pytestmark = pytest.mark.unit

_TS = 1793585542519320001


def _event(ticket_dir: Path, event_type: str, data: dict, *, ts: int) -> None:
    """Append one event file, the way the store itself lays them out."""
    event_uuid = str(uuid.uuid4())
    payload = {
        "uuid": event_uuid,
        "env_id": "test-env",
        "timestamp": ts,
        "event_type": event_type,
        "author": "t",
        "author_email": "t@e.com",
        "data": data,
    }
    path = ticket_dir / f"{ts}-{event_uuid}-{event_type}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_ticket(tracker: Path, tid: str, title: str, description: str = "") -> Path:
    ticket_dir = tracker / tid
    ticket_dir.mkdir(parents=True)
    _event(
        ticket_dir,
        "CREATE",
        {
            "id": tid,
            "alias": f"alias-for-{tid}",
            "title": title,
            "description": description,
            "ticket_type": "task",
            "priority": 2,
            "tags": [],
            "parent_id": "",
            "creation_channel": "python",
        },
        ts=_TS,
    )
    return ticket_dir


def _retitle(ticket_dir: Path, title: str) -> None:
    _event(ticket_dir, "EDIT", {"fields": {"title": title}}, ts=_TS + 1000)


@pytest.fixture
def tracker(tmp_path: Path) -> Path:
    """An empty store directory. The index lands in its sibling ``.rebar``."""
    path = tmp_path / "store" / ".tickets-tracker"
    path.mkdir(parents=True)
    return path


def _indexed(tracker: Path) -> sqlite3.Connection:
    conn = open_index(tracker)
    assert conn is not None
    catch_up(tracker, conn)
    return conn


# --- ranking -----------------------------------------------------------------------


def test_bm25_weights_align_with_the_indexed_column_order() -> None:
    """The weights are positional, so a column added without a weight silently reranks."""
    assert [c.strip() for c in BM25_WEIGHTS.split(",")] == ["0.0", "0.0", "5.0", "1.0"]
    columns = [c.strip().split()[0] for c in _FTS_COLUMNS.split(",")]
    assert columns == ["ticket_id", "alias", "title", "description"]


def test_a_title_match_outranks_a_description_match(tracker: Path) -> None:
    """Title weighted 5.0 against description 1.0, asserted through the ranking itself."""
    _write_ticket(tracker, "1111-1111-1111-1111", "quota exhaustion", "unrelated body text")
    _write_ticket(
        tracker,
        "2222-2222-2222-2222",
        "unrelated heading",
        "quota exhaustion quota exhaustion quota exhaustion",
    )
    conn = _indexed(tracker)
    results = search(conn, "quota exhaustion")
    assert results[0]["id"] == "1111-1111-1111-1111"


def test_a_query_returns_at_most_the_result_limit(tracker: Path) -> None:
    for n in range(RESULT_LIMIT + 3):
        _write_ticket(tracker, f"{n:04d}-aaaa-bbbb-cccc", "shared matching heading")
    conn = _indexed(tracker)
    assert len(search(conn, "shared matching heading")) == RESULT_LIMIT


def test_each_result_carries_the_published_keys(tracker: Path) -> None:
    _write_ticket(tracker, "1111-1111-1111-1111", "indexable heading", "body")
    conn = _indexed(tracker)
    (result,) = search(conn, "indexable heading")
    assert sorted(result) == ["alias", "id", "score", "title"]
    assert result["id"] == "1111-1111-1111-1111"
    assert result["alias"] == "alias-for-1111-1111-1111-1111"
    assert result["title"] == "indexable heading"


def test_every_score_is_within_zero_and_one(tracker: Path) -> None:
    for n in range(4):
        _write_ticket(tracker, f"{n:04d}-aaaa-bbbb-cccc", f"budget overrun {n}", "shared body")
    conn = _indexed(tracker)
    scores = [r["score"] for r in search(conn, "budget overrun shared body")]
    assert scores
    assert all(0.0 <= s <= 1.0 for s in scores)


def test_the_top_result_holds_the_maximum_score(tracker: Path) -> None:
    """Raw bm25 ranks ascending; the published score must rank the other way."""
    _write_ticket(tracker, "1111-1111-1111-1111", "cache eviction policy", "exact match body")
    for n in range(3):
        _write_ticket(tracker, f"{n:04d}-aaaa-bbbb-cccc", "cache", "loosely related body")
    conn = _indexed(tracker)
    scores = [r["score"] for r in search(conn, "cache eviction policy")]
    assert len(scores) > 1
    assert scores[0] == max(scores)


def test_an_excluded_id_never_appears_in_its_own_results(tracker: Path) -> None:
    """A create queries with its own text, which catch-up has already indexed."""
    _write_ticket(tracker, "1111-1111-1111-1111", "retry backoff", "jittered retry backoff")
    _write_ticket(tracker, "2222-2222-2222-2222", "retry backoff", "a second jittered one")
    conn = _indexed(tracker)
    text = query_text("retry backoff", "jittered retry backoff")
    assert "1111-1111-1111-1111" in {r["id"] for r in search(conn, text)}
    excluded = search(conn, text, exclude_id="1111-1111-1111-1111")
    assert "1111-1111-1111-1111" not in {r["id"] for r in excluded}
    assert "2222-2222-2222-2222" in {r["id"] for r in excluded}


def test_an_exclusion_does_not_shorten_a_full_result_set(tracker: Path) -> None:
    """One extra row is fetched, so dropping the self-match still fills the limit."""
    for n in range(RESULT_LIMIT + 1):
        _write_ticket(tracker, f"{n:04d}-aaaa-bbbb-cccc", "shared matching heading")
    conn = _indexed(tracker)
    results = search(conn, "shared matching heading", exclude_id="0000-aaaa-bbbb-cccc")
    assert len(results) == RESULT_LIMIT
    assert "0000-aaaa-bbbb-cccc" not in {r["id"] for r in results}


def test_query_text_joins_the_title_to_the_head_of_the_description() -> None:
    text = query_text("heading", "x" * 500)
    assert text.startswith("heading ")
    assert len(text) == len("heading ") + 200


def test_match_expression_drops_punctuation_fts5_would_read_as_an_operator() -> None:
    """Raw prose reaches MATCH, and a bare ``-`` or quote is a syntax error there."""
    assert match_expression('rollback - "failed" migration:now') == (
        '"rollback" OR "failed" OR "migration" OR "now"'
    )


def test_a_query_with_no_usable_tokens_matches_nothing_rather_than_raising(
    tracker: Path,
) -> None:
    _write_ticket(tracker, "1111-1111-1111-1111", "indexable heading")
    conn = _indexed(tracker)
    assert search(conn, "--- :: ***") == []


# --- catch-up ----------------------------------------------------------------------


def test_catch_up_indexes_a_ticket_directory_created_after_the_last_build(
    tracker: Path,
) -> None:
    """Enumerating the live set is what makes a NEW ticket discoverable."""
    conn = _indexed(tracker)
    assert search(conn, "late arrival") == []
    _write_ticket(tracker, "1111-1111-1111-1111", "late arrival", "added after the build")
    assert catch_up(tracker, conn)["indexed"] == 1
    assert [r["id"] for r in search(conn, "late arrival")] == ["1111-1111-1111-1111"]


def test_catch_up_drops_an_entry_whose_directory_is_gone(tracker: Path) -> None:
    ticket_dir = _write_ticket(tracker, "1111-1111-1111-1111", "transient ticket")
    conn = _indexed(tracker)
    assert search(conn, "transient ticket")
    for child in ticket_dir.iterdir():
        child.unlink()
    ticket_dir.rmdir()
    assert catch_up(tracker, conn)["dropped"] == 1
    assert search(conn, "transient ticket") == []


def test_catch_up_reindexes_a_ticket_whose_dir_hash_changed(tracker: Path) -> None:
    ticket_dir = _write_ticket(tracker, "1111-1111-1111-1111", "ptarmigan dispatch")
    conn = _indexed(tracker)
    before = dir_hash_of(str(ticket_dir))
    _retitle(ticket_dir, "wolverine dispatch")
    assert dir_hash_of(str(ticket_dir)) != before
    assert catch_up(tracker, conn)["indexed"] == 1
    assert [r["id"] for r in search(conn, "wolverine")] == ["1111-1111-1111-1111"]
    assert search(conn, "ptarmigan") == []


def test_reconciling_one_ticket_leaves_every_other_indexed_ticket_intact(
    tracker: Path,
) -> None:
    """In-place reconciliation touches only the rows the diff named."""
    _write_ticket(tracker, "1111-1111-1111-1111", "ptarmigan")
    changing = _write_ticket(tracker, "2222-2222-2222-2222", "wolverine")
    _write_ticket(tracker, "3333-3333-3333-3333", "capybara")
    conn = _indexed(tracker)

    _retitle(changing, "pangolin")
    assert catch_up(tracker, conn)["indexed"] == 1

    assert [r["id"] for r in search(conn, "ptarmigan")] == ["1111-1111-1111-1111"]
    assert [r["id"] for r in search(conn, "capybara")] == ["3333-3333-3333-3333"]
    assert [r["id"] for r in search(conn, "pangolin")] == ["2222-2222-2222-2222"]
    assert search(conn, "wolverine") == []


def test_catch_up_does_no_work_on_an_unchanged_store(tracker: Path) -> None:
    """The warm path: an empty diff opens no transaction and writes nothing."""
    _write_ticket(tracker, "1111-1111-1111-1111", "steady heading")
    conn = _indexed(tracker)
    assert catch_up(tracker, conn) == {"indexed": 0, "dropped": 0, "scanned": 1}


def test_a_touched_but_identical_directory_is_rehashed_and_not_reindexed(
    tracker: Path,
) -> None:
    """The mtime only permits the exact check; the HASH decides."""
    ticket_dir = _write_ticket(tracker, "1111-1111-1111-1111", "steady heading")
    conn = _indexed(tracker)
    stat = os.stat(ticket_dir)
    os.utime(ticket_dir, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert catch_up(tracker, conn)["indexed"] == 0
    assert [r["id"] for r in search(conn, "steady heading")] == ["1111-1111-1111-1111"]


def test_live_entries_reports_one_mtime_per_ticket_directory(tracker: Path) -> None:
    ticket_dir = _write_ticket(tracker, "1111-1111-1111-1111", "heading")
    entries = live_entries(tracker)
    assert entries == {"1111-1111-1111-1111": os.stat(ticket_dir).st_mtime_ns}


def test_a_directory_that_will_not_reduce_is_recorded_without_a_document(
    tracker: Path,
) -> None:
    """A dir with no usable events must not be rescanned forever, nor be indexed."""
    (tracker / "4444-4444-4444-4444").mkdir()
    conn = _indexed(tracker)
    assert catch_up(tracker, conn) == {"indexed": 0, "dropped": 0, "scanned": 1}


def test_a_settled_index_finds_work_again_once_one_dirstate_row_is_falsified(
    tracker: Path,
) -> None:
    """The negative control for the settled-index claim the scale suite asserts.

    BOTH columns have to be falsified. ``_changed`` skips any directory whose stored
    ``dir_mtime_ns`` still equals the live one and never hashes it, so a wrong
    ``dir_hash`` alone is unreachable and the control would pass while demonstrating
    nothing. A stale mtime alone reaches the hash, which then matches, so it re-hashes
    and still indexes nothing. Only the pair makes the index re-write the document.
    """
    _write_ticket(tracker, "1111-1111-1111-1111", "ptarmigan")
    conn = _indexed(tracker)
    try:
        assert catch_up(tracker, conn) == {"indexed": 0, "dropped": 0, "scanned": 1}
        conn.execute(
            "UPDATE dirstate SET dir_mtime_ns = 0, dir_hash = 'not-the-hash' WHERE dir_name = ?",
            ("1111-1111-1111-1111",),
        )
        conn.commit()
        assert catch_up(tracker, conn) == {"indexed": 1, "dropped": 0, "scanned": 1}
    finally:
        conn.close()


# --- failure disposition -----------------------------------------------------------


def test_a_query_returns_empty_rather_than_raising_when_the_index_is_absent(
    tracker: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="rebar.similar.index"):
        conn = open_index(tracker)
    assert conn is not None
    conn.close()
    assert any("absent" in r.message for r in caplog.records if r.levelno >= logging.WARNING)


def test_a_query_returns_empty_rather_than_raising_when_catch_up_raises(
    tracker: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _write_ticket(tracker, "1111-1111-1111-1111", "indexable heading")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("catch-up exploded")

    monkeypatch.setattr("rebar.similar.catch_up", _boom)
    with caplog.at_level(logging.WARNING, logger="rebar.similar"):
        assert find_similar(str(tracker), title="indexable heading") == []
    assert any("catch-up failed" in r.message for r in caplog.records)


def test_a_corrupt_index_is_discarded_rebuilt_and_queryable(
    tracker: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_ticket(tracker, "1111-1111-1111-1111", "survives the rebuild")
    _indexed(tracker).close()
    path = index_path(tracker)
    Path(path).write_bytes(b"this is definitively not a sqlite database" * 32)

    with caplog.at_level(logging.WARNING, logger="rebar.similar.index"):
        conn = open_index(tracker)
    assert conn is not None
    assert any("unusable" in r.message for r in caplog.records)

    # The rebuilt index starts empty, and catch-up refills it from the store.
    catch_up(tracker, conn)
    assert [r["id"] for r in search(conn, "survives the rebuild")] == ["1111-1111-1111-1111"]


def test_an_index_from_an_older_version_is_discarded_and_rebuilt(
    tracker: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A schema change must not be read as a healthy index that matched nothing."""
    _write_ticket(tracker, "1111-1111-1111-1111", "survives the version bump")
    conn = _indexed(tracker)
    conn.execute("UPDATE meta SET value = ? WHERE key = 'version'", (str(INDEX_VERSION - 1),))
    conn.close()

    with caplog.at_level(logging.WARNING, logger="rebar.similar.index"):
        rebuilt = open_index(tracker)
    assert rebuilt is not None
    assert any("unusable" in r.message for r in caplog.records)
    catch_up(tracker, rebuilt)
    assert [r["id"] for r in search(rebuilt, "survives the version bump")] == [
        "1111-1111-1111-1111"
    ]


def test_find_similar_returns_empty_rather_than_raising_when_the_index_cannot_open(
    tracker: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _unavailable(*_args: object, **_kwargs: object) -> None:
        raise OSError("cache directory is read-only")

    monkeypatch.setattr("rebar.similar.open_index", _unavailable)
    with caplog.at_level(logging.WARNING, logger="rebar.similar"):
        assert find_similar(str(tracker), title="anything at all") == []
    assert any("retrieval failed" in r.message for r in caplog.records)


def test_find_similar_serves_a_ticket_created_since_the_last_query(tracker: Path) -> None:
    """Catch-up runs in the read path, so no daemon or background thread is needed."""
    assert find_similar(str(tracker), title="just created") == []
    _write_ticket(tracker, "1111-1111-1111-1111", "just created", "written between queries")
    results = find_similar(str(tracker), title="just created")
    assert [r["id"] for r in results] == ["1111-1111-1111-1111"]


# --- concurrency -------------------------------------------------------------------


def test_concurrent_catch_up_runs_leave_a_sound_index_holding_both_tickets(
    tracker: Path,
) -> None:
    """Reconcilers serialize on sqlite's write lock; a loser redoes rather than merges."""
    _write_ticket(tracker, "1111-1111-1111-1111", "ptarmigan")
    _write_ticket(tracker, "2222-2222-2222-2222", "wolverine")

    failures: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _run() -> None:
        try:
            conn = open_index(tracker)
            assert conn is not None
            try:
                barrier.wait(timeout=30)
                catch_up(tracker, conn)
            finally:
                conn.close()
        except BaseException as exc:  # noqa: BLE001 - reported on the main thread
            failures.append(exc)

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()
    assert not failures

    conn = open_index(tracker)
    assert conn is not None
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert [r["id"] for r in search(conn, "ptarmigan")] == ["1111-1111-1111-1111"]
    assert [r["id"] for r in search(conn, "wolverine")] == ["2222-2222-2222-2222"]
    conn.close()


def _make_stale(tracker: Path) -> None:
    """Leave a readable index whose recorded version no longer matches the code's.

    This is the version-bump case rather than a torn file, and it is the one that
    matters: the moment ``INDEX_VERSION`` moves, every existing index on every box
    takes the recovery branch at once. That is what turns the race from reachable
    into expected.
    """
    conn = sqlite3.connect(index_path(tracker), isolation_level=None)
    try:
        conn.execute("UPDATE meta SET value = ? WHERE key = 'version'", (str(INDEX_VERSION - 1),))
    finally:
        conn.close()


def _recover_concurrently(
    tracker: Path, monkeypatch: pytest.MonkeyPatch, count: int = 2
) -> tuple[list[BaseException], list[str]]:
    """Drive ``count`` callers into recovery at once, holding the window open.

    A plain barrier at the start of ``open_index`` is not enough. The first caller
    discards and rebuilds in microseconds, so the second opens a healthy index and
    never enters recovery at all -- the interleaving under test simply does not
    happen, and an unsynchronized implementation passes. So the first caller to
    reach ``_discard`` pauses there, before unlinking, which holds the second caller
    inside recovery while the file is still being destroyed. That pause is the
    experiment; without it there is nothing to observe.

    Returns the threads' failures and every path passed to ``_discard``.
    """
    failures: list[BaseException] = []
    discards: list[str] = []
    counted = threading.Lock()
    real_discard = similar.index._discard

    def _instrumented_discard(path: str) -> None:
        with counted:
            first = not discards
            discards.append(path)
        if first:
            time.sleep(0.5)
        real_discard(path)

    monkeypatch.setattr(similar.index, "_discard", _instrumented_discard)
    barrier = threading.Barrier(count)

    def _run() -> None:
        try:
            barrier.wait(timeout=30)
            conn = open_index(tracker)
            assert conn is not None, "recovery returned no connection"
            conn.close()
        except BaseException as exc:  # noqa: BLE001 - reported on the main thread
            failures.append(exc)

    threads = [threading.Thread(target=_run) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()
    return failures, discards


def test_concurrent_recovery_leaves_a_sound_index_and_loses_no_ticket(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two callers discarding and rebuilding at once must not detach each other's file."""
    _write_ticket(tracker, "1111-1111-1111-1111", "ptarmigan")
    _write_ticket(tracker, "2222-2222-2222-2222", "wolverine")
    _indexed(tracker).close()
    _make_stale(tracker)

    failures, _ = _recover_concurrently(tracker, monkeypatch)
    assert not failures

    conn = open_index(tracker)
    assert conn is not None
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert [r["id"] for r in find_similar(str(tracker), title="ptarmigan")] == [
        "1111-1111-1111-1111"
    ]
    assert [r["id"] for r in find_similar(str(tracker), title="wolverine")] == [
        "2222-2222-2222-2222"
    ]


def test_only_one_caller_discards_when_two_enter_recovery_on_one_stale_index(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock alone would not be enough; the re-check is what stops the second discard.

    Unsynchronized, both callers unlink, and the second unlinks the database the first
    had already rebuilt. Counting the discards is the cheapest observation of that: it
    reports 2 against the unsynchronized implementation and 1 against this one.
    """
    _write_ticket(tracker, "1111-1111-1111-1111", "ptarmigan")
    _indexed(tracker).close()
    _make_stale(tracker)

    failures, discards = _recover_concurrently(tracker, monkeypatch)
    assert not failures
    assert len(discards) == 1


# --- advisory findings from the pre-push code review ---------------------------------


def test_a_limit_of_zero_returns_nothing_rather_than_one_row(tracker: Path) -> None:
    """The truncation loop breaks only AFTER an append, so zero needs its own guard."""
    _write_ticket(tracker, "3333-3333-3333-3333", "capercaillie")
    conn = open_index(tracker)
    assert conn is not None
    catch_up(tracker, conn)
    try:
        assert search(conn, "capercaillie") != []
        assert search(conn, "capercaillie", limit=0) == []
        assert search(conn, "capercaillie", limit=-1) == []
    finally:
        conn.close()


def test_an_unreadable_directory_mtime_never_counts_as_unchanged(tracker: Path) -> None:
    """Two UNKNOWN_MTIME sentinels say nothing about whether the directory moved.

    Comparing them by equality would skip the exact hash check, freezing whatever
    document the directory last produced for as long as the stat keeps failing.
    """
    name = "4444-4444-4444-4444"
    stored = {name: (UNKNOWN_MTIME, "a-hash-from-an-earlier-run")}

    changed = _changed(str(tracker), {name: UNKNOWN_MTIME}, stored)

    assert name in changed, "an unknown mtime must fall through to the hash check"


def test_a_close_that_raises_does_not_escape_find_similar(
    tracker: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``find_similar`` documents 'Never raises', and the close is the last place to break it."""
    _write_ticket(tracker, "5555-5555-5555-5555", "guillemot")

    real_open = similar.open_index

    class _ClosesBadly:
        """Delegates everything to the real connection except ``close``, which raises.

        ``sqlite3.Connection.close`` is read-only, so it cannot be monkeypatched in
        place; a proxy is the only way to reach the branch.
        """

        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn

        def __getattr__(self, name: str) -> object:
            return getattr(self._conn, name)

        def close(self) -> None:
            self._conn.close()
            raise sqlite3.OperationalError("close failed")

    def _open_with_a_failing_close(*args: object, **kwargs: object) -> object:
        conn = real_open(*args, **kwargs)
        assert conn is not None
        return _ClosesBadly(conn)

    monkeypatch.setattr(similar, "open_index", _open_with_a_failing_close)

    with caplog.at_level(logging.WARNING):
        results = find_similar(str(tracker), title="guillemot", description="")

    assert [r["id"] for r in results] == ["5555-5555-5555-5555"]
    assert "index close failed" in caplog.text
