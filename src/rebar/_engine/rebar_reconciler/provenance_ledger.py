"""ProvenanceLedger — per-element provenance tracking for conflict resolution.

Records the side (local|jira) that last wrote each element, plus a timestamp.
Provides a stateless is_echo() check via content-hash equality — used to
distinguish "the same value just came back from the other side" from "a
genuine new write." JSON-serializable via to_dict().
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _content_hash(value: Any) -> str:
    """Stable content hash for echo detection. Same input → same hash."""
    serialized = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass
class ProvenanceLedger:
    """Per-element provenance ledger.

    record(key, side, value) — append an entry recording that `side` wrote
    `value` under `key` at the current UTC time.
    is_echo(key, value) — stateless content-equality check: returns True iff
    the most recent entry for `key` carries the identical value (by content
    hash). Does not consult record() history beyond hash comparison.
    to_dict() — return a JSON-serializable dict of all entries.
    """
    entries: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def record(self, key: str, side: str, value: Any) -> None:
        """Append an entry. `side` must be 'local' or 'jira'."""
        if side not in ("local", "jira"):
            raise ValueError(f"side must be 'local' or 'jira', got {side!r}")
        timestamp = datetime.now(timezone.utc).isoformat()
        entry = {
            "side": side,
            "timestamp": timestamp,
            "value_hash": _content_hash(value),
        }
        self.entries.setdefault(key, []).append(entry)

    def is_echo(self, key: str, value: Any) -> bool:
        """Stateless content-equality check — last entry's value_hash == hash(value)."""
        history = self.entries.get(key, [])
        if not history:
            return False
        return history[-1]["value_hash"] == _content_hash(value)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict form. All values are str / list / dict."""
        return {
            "schema_version": 1,
            "entries": dict(self.entries),
        }
