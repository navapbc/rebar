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

``vectors``
    The opt-in embedding tier's storage, one row per ticket: the embedded digest's
    vector plus the VALIDITY KEY it was produced under. Embedding spaces are not
    comparable across models, so the key folds in the embedding-model identity and
    the pinned digest-prompt commit; a row whose key is not the current one is not a
    usable vector and is simply ignored. See :mod:`rebar.similar.embedding`.

The index is a regenerable cache: a reader that finds it corrupt discards and
rebuilds it rather than raising, so no eviction path is needed.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from array import array
from pathlib import Path

from rebar._snapshot.git_fetch import interprocess_lock

logger = logging.getLogger(__name__)

#: Bumped when the schema or the indexed text changes; a mismatch rebuilds.
INDEX_VERSION = 3

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
    conn.execute(
        "CREATE TABLE IF NOT EXISTS vectors ("
        "  ticket_id TEXT PRIMARY KEY,"
        "  validity_key TEXT NOT NULL,"
        "  vector BLOB NOT NULL"
        ")"
    )
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


def _recover(path: str) -> sqlite3.Connection | None:
    """Discard and rebuild one unusable index, serialized across callers.

    Every other write in this subsystem serializes on sqlite's own write lock, but
    ``_discard`` unlinks files, and ``os.unlink`` is not covered by that lock. Two
    callers that both found the index unusable would otherwise interleave: one can
    unlink the WAL sidecar of the other's live connection, or commit rows into an
    inode the other has already detached. Both leave a silently frozen index, which
    is indistinguishable from a healthy index that matched nothing.

    So the recovery is single-flight, on the same lock-then-RE-CHECK pattern
    ``rebar._snapshot.cache`` runs for snapshot materialization: a peer may have
    finished rebuilding while this caller waited, so retry the open inside the lock
    BEFORE unlinking anything. The re-check is what removes the harm -- a lock alone
    would still let the second caller discard a database the first just rebuilt.

    This cannot help a caller that opened the index successfully *before* a peer
    entered recovery; that caller never came here. Discarding a genuinely corrupt
    file is destructive by nature. What this buys is that exactly one caller
    discards, and that no caller discards an index that is already healthy.
    """
    try:
        # os.unlink is not covered by sqlite's write lock, so single-flight recovery
        # cannot reuse it. Adding this lock IS the ticket; there is no narrower fix.
        # mechanism-ok: lock .recover.lock — 4932-ea62-cf4f-451b: unlink escapes sqlite
        with interprocess_lock(Path(path + ".recover.lock")):
            try:
                return _open_one(path)
            except (sqlite3.DatabaseError, StaleIndex):
                pass  # still unusable: this caller is the one that rebuilds it
            _discard(path)
            try:
                return _open_one(path)
            except (sqlite3.DatabaseError, OSError, StaleIndex) as exc:
                logger.warning("similar-ticket index could not be rebuilt: %s", exc)
                return None
    except OSError as exc:
        logger.warning("similar-ticket index unavailable: %s", exc)
        return None


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
        return _recover(path)
    except OSError as exc:
        logger.warning("similar-ticket index unavailable: %s", exc)
        return None


# ── Embedding-tier vector storage ─────────────────────────────────────────────
#
# These are deliberately the whole storage surface for the opt-in embedding tier:
# `rebar.similar.embedding` owns the digest, the provider calls and the ranking, and
# never writes sql of its own. Keeping the schema in one module is what lets
# `INDEX_VERSION` remain the single answer to "is this file's shape current".

#: The vector element type. float32 halves the stored size against float64 and is
#: far below the precision at which a cosine ORDERING over 1,024 dimensions changes.
_VECTOR_TYPECODE = "f"


def validity_key(embedding_model: str, digest_prompt_commit: str) -> str:
    """The key a stored vector is only comparable under.

    Embedding spaces are not comparable across models, and a digest-prompt change
    alters the text that was embedded, so BOTH identities belong in the key. Folding
    them into one opaque digest rather than storing two columns means a future third
    input is a change to this function alone, and means no caller can compare one
    half of the identity and believe it checked the whole.
    """
    material = f"{embedding_model}\x00{digest_prompt_commit}".encode()
    return hashlib.sha256(material).hexdigest()


def store_vector(conn: sqlite3.Connection, ticket_id: str, key: str, vector: list[float]) -> None:
    """Persist one ticket's vector under *key*, replacing any earlier row."""
    blob = array(_VECTOR_TYPECODE, [float(v) for v in vector]).tobytes()
    conn.execute(
        "INSERT INTO vectors (ticket_id, validity_key, vector) VALUES (?, ?, ?) "
        "ON CONFLICT(ticket_id) DO UPDATE SET "
        "  validity_key = excluded.validity_key, vector = excluded.vector",
        (ticket_id, key, sqlite3.Binary(blob)),
    )


def drop_vectors(conn: sqlite3.Connection, ticket_ids) -> None:
    """Remove the vectors for *ticket_ids*. Called wherever a document is rewritten
    or dropped: the stored vector describes text that no longer exists, and keeping it
    would rank a ticket by its previous content."""
    conn.executemany("DELETE FROM vectors WHERE ticket_id = ?", [(t,) for t in ticket_ids])


def current_vectors(conn: sqlite3.Connection, key: str) -> dict[str, list[float]]:
    """Every stored vector carrying *key*, as ``{ticket_id: vector}``.

    Rows under any other key are not returned at all. They are not stale data to be
    repaired here -- they are vectors in a DIFFERENT embedding space, and the only
    safe thing to do with them at read time is to not see them.
    """
    rows = conn.execute(
        "SELECT ticket_id, vector FROM vectors WHERE validity_key = ?", (key,)
    ).fetchall()
    return {row[0]: list(array(_VECTOR_TYPECODE, bytes(row[1]))) for row in rows}


def unvectored_ids(conn: sqlite3.Connection, key: str) -> list[str]:
    """The indexed tickets with no current-key vector.

    The embedding tier ranks only when this is empty. A partial corpus cannot be
    cosine-ranked without silently dropping every ticket the build has not reached,
    which is indistinguishable from those tickets being irrelevant.
    """
    rows = conn.execute(
        "SELECT ticket_id FROM docs WHERE ticket_id NOT IN "
        "(SELECT ticket_id FROM vectors WHERE validity_key = ?)",
        (key,),
    ).fetchall()
    return [row[0] for row in rows]
