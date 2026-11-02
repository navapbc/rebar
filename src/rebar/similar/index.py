"""The sqlite FTS5 index backing similar-ticket retrieval.

The index lives under the git-ignored cache dir, mirroring the plan-review checkpoint
cache (``rebar.llm.plan_review.checkpoints``). Nothing derived ever reaches the
``tickets`` branch.

Two tables carry the whole design:

``docs``
    An FTS5 virtual table whose INDEXED columns are exactly ``title`` and
    ``description`` -- the shape ADR 0122 measured. ``ticket_id`` and ``alias`` ride
    along UNINDEXED so serving a query needs no ticket-store read; being unindexed
    they contribute nothing to ranking.

``dirstate``
    The persisted ticket-directory -> last-indexed state map. It is what makes
    catch-up cheap, and diffing the live directory set against it is what makes a
    NEW ticket discoverable -- ``compute_dir_hash`` detects change within a known
    directory but cannot discover one that was not there before.

    Each row carries BOTH the directory's own mtime and its exact dir hash. The
    mtime is a cheap pre-filter and the hash is the authority; see
    :mod:`rebar.similar.catchup` for why one stat per DIRECTORY rather than one per
    event file is what makes the warm path affordable.

The index is a regenerable cache: a reader that finds it corrupt discards and
rebuilds it rather than raising, so no eviction path is needed.
"""

from __future__ import annotations

import logging
import os
import sqlite3

logger = logging.getLogger(__name__)

#: Bumped when the schema or the indexed text changes; a mismatch rebuilds.
INDEX_VERSION = 2

#: Seconds a writer waits on sqlite's write lock before giving up. Concurrent
#: reconcilers serialize here rather than corrupting each other.
BUSY_TIMEOUT_S = 30.0

_CACHE_SUBDIR = ("cache", "similar-tickets")
_INDEX_FILENAME = "index.sqlite3"

#: FTS5 column order. Weights supplied to ``bm25()`` must match it positionally.
_FTS_COLUMNS = "ticket_id UNINDEXED, alias UNINDEXED, title, description"

#: ADR 0122's measured weighting -- title 5.0, description 1.0 -- with 0.0 for each
#: UNINDEXED column so the ranking is the one the ADR adopted.
BM25_WEIGHTS = "0.0, 0.0, 5.0, 1.0"


class StaleIndex(Exception):
    """A readable index built by an incompatible version. Treated exactly as corruption."""


def index_dir(tracker: str | os.PathLike[str]) -> str:
    """The git-ignored directory holding the index, derived from the store's ``.rebar``."""
    from rebar._store.paths import StorePaths

    return os.path.join(StorePaths(str(tracker)).rebar_dir, *_CACHE_SUBDIR)


def index_path(tracker: str | os.PathLike[str]) -> str:
    """The index database file."""
    return os.path.join(index_dir(tracker), _INDEX_FILENAME)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the tables when absent. Safe to call on every open."""
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5({_FTS_COLUMNS})")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dirstate ("
        "  dir_name TEXT PRIMARY KEY,"
        "  dir_mtime_ns INTEGER NOT NULL,"
        "  dir_hash TEXT NOT NULL"
        ")"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    row = conn.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta (key, value) VALUES ('version', ?)", (str(INDEX_VERSION),))
    elif row[0] != str(INDEX_VERSION):
        raise StaleIndex(f"index version {row[0]} != {INDEX_VERSION}")


def _discard(path: str) -> None:
    """Remove the index and its WAL sidecars so the next open rebuilds from empty."""
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def _open_one(path: str) -> sqlite3.Connection:
    """Open, configure and schema-check one connection. Raises on a corrupt file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_S * 1000)}")
        _ensure_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def open_index(tracker: str | os.PathLike[str]) -> sqlite3.Connection | None:
    """A usable connection, rebuilding from empty if the existing file is unusable.

    Returns ``None`` only when even a fresh index cannot be opened -- a read-only or
    unwritable cache directory. Retrieval treats that as "no results", never as an
    error, so an advisory can never fail its caller.

    A FIRST build is reported at warning level. It is a once-per-clone event, and
    announcing it is what keeps "the index was absent" distinguishable from "the
    index was consulted and genuinely matched nothing" -- two states that are
    otherwise identical from the outside, since both yield no results.
    """
    path = index_path(tracker)
    absent = not os.path.exists(path)
    try:
        conn = _open_one(path)
        if absent:
            logger.warning("similar-ticket index absent at %s; building it", path)
        return conn
    except (sqlite3.DatabaseError, StaleIndex) as exc:
        # A corrupt or incompatible cache is regenerable, so discard rather than raise.
        # Logged at warning: without it the index degrades to permanently empty results
        # with no observable symptom.
        logger.warning("similar-ticket index unusable (%s); rebuilding", exc)
        _discard(path)
    except OSError as exc:
        logger.warning("similar-ticket index unavailable: %s", exc)
        return None
    try:
        return _open_one(path)
    except (sqlite3.DatabaseError, OSError, StaleIndex) as exc:
        logger.warning("similar-ticket index could not be rebuilt: %s", exc)
        return None
