"""Structured JSON-line logger for dso_reconciler sync operations.

Writes one JSON object per line to a log file.  Each entry carries a UTC
timestamp and an event type, plus event-specific keyword fields.

Usage::

    logger = SyncLogger(Path("bridge_state/sync-log-2026-01-01T00-00-00.jsonl"))
    logger.log("sync_pass_start", pass_id="2026-01-01T00-00-00", mode="live")
    ...
    logger.close()

The log file is opened in append mode so multiple passes can share a file
if needed, though the normal convention is one file per pass_id.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SyncLogger:
    """Append-only JSON-lines logger for reconciler sync events."""

    def __init__(self, log_path: Path) -> None:
        self._path = log_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(log_path, "a")  # noqa: SIM115

    def log(self, event_type: str, **kwargs: Any) -> None:
        """Write a structured JSON log line.

        Args:
            event_type: One of the canonical event types (see module docstring).
            **kwargs:   Event-specific fields merged into the log entry.
        """
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z",
            "event": event_type,
            **kwargs,
        }
        self._f.write(json.dumps(entry, default=str) + "\n")
        self._f.flush()

    # -- context manager protocol ---------------------------------------------

    def __enter__(self) -> SyncLogger:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        """Flush and close the underlying file handle."""
        self._f.close()
