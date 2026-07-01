"""Idempotency / dedup store for cast votes (epic d251 / S4b).

Gerrit's ``webhooks`` plugin delivers at-least-once (``maxTries=5``,
``retryInterval=1000ms``), and because an LLM review blows the ~5s webhook socket
timeout the SAME ``patchset-created`` event is re-delivered while the first review is
still running. The backfill reconciler can also pick up the same patchset. To never
double-vote we record a row per ``(change_id, revision)`` AFTER a confirmed-successful
vote (write-on-success) and short-circuit on the next sighting.

Single-box appropriate: a small SQLite file on the box's data volume, opened in WAL
mode so the webhook worker and the reconciler can read/write concurrently. The Gerrit
side remains AUTHORITATIVE — ``voter`` also checks for an existing ``LLM-Review`` vote
on the current revision (``gerrit_client.has_llm_review_vote``) — so a lost dedup row
(e.g. a fresh box) still cannot double-vote.

Plain stdlib (``sqlite3``); imports without ``fastapi`` or the ``agents`` extra.
"""

from __future__ import annotations

import os
import sqlite3
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS voted (
    change_id  TEXT    NOT NULL,
    revision   TEXT    NOT NULL,
    event_type TEXT,
    vote_value INTEGER,
    voted_at   INTEGER,
    PRIMARY KEY (change_id, revision)
);
"""


class DedupStore:
    """A tiny SQLite-backed (change_id, revision) → cast-vote ledger."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        # ``isolation_level=None`` → autocommit; each statement is its own transaction,
        # which keeps the write-on-success record durable the moment it lands.
        conn = sqlite3.connect(self._db_path, isolation_level=None, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    def already_voted(self, change_id: str, revision: str) -> bool:
        """True if a vote for this ``(change_id, revision)`` has been recorded."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM voted WHERE change_id=? AND revision=? LIMIT 1",
                (change_id, revision),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def record_vote(
        self,
        change_id: str,
        revision: str,
        event_type: str | None,
        vote_value: int,
    ) -> None:
        """Record a SUCCESSFULLY-cast vote. Call ONLY after Gerrit confirmed the vote
        (write-on-success): a failed vote must leave NO row so a retry re-attempts it.
        Idempotent — a duplicate (change_id, revision) is upserted, not duplicated."""
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO voted (change_id, revision, event_type, vote_value, voted_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(change_id, revision) DO UPDATE SET "
                "event_type=excluded.event_type, vote_value=excluded.vote_value, "
                "voted_at=excluded.voted_at",
                (change_id, revision, event_type, int(vote_value), int(time.time())),
            )
        finally:
            conn.close()
