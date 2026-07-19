"""Append-only NDJSON snapshot store for going-forward code-health metrics.

Point-in-time metric records are appended, one JSON object per line, to the
git-tracked ``<repo_root>/.rebar/metrics-snapshots.ndjson`` file so the series
persists and is shared like any other tracked artifact. Each stored line wraps
the caller's record with the ISO-8601 timestamp it was taken at; readers query a
closed ``[since, until]`` timestamp range and get the *original* record dicts
back. Malformed or truncated lines are skipped rather than raising, so a partial
write never breaks a read.

Public surface:
- ``write_snapshot(record, *, repo_root, ts)`` — append one timestamped record.
- ``read_snapshots(since, until, *, repo_root)`` — records within ``[since, until]``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

_SNAPSHOT_RELPATH = os.path.join(".rebar", "metrics-snapshots.ndjson")


def _snapshot_path(repo_root: str | os.PathLike[str]) -> Path:
    return Path(repo_root) / _SNAPSHOT_RELPATH


def _to_naive_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp (aware or naive, possibly date-only) to a
    naive UTC ``datetime`` so bounds and record stamps compare uniformly."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def write_snapshot(record: dict, *, repo_root: str | os.PathLike[str], ts: str) -> None:
    """Append ``record`` — tagged with ISO-8601 timestamp ``ts`` — as one JSON
    line to ``<repo_root>/.rebar/metrics-snapshots.ndjson``, creating the
    ``.rebar/`` directory and file if they do not yet exist."""
    path = _snapshot_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": ts, "record": record}, sort_keys=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def read_snapshots(since: str, until: str, *, repo_root: str | os.PathLike[str]) -> list[dict]:
    """Return the original record dicts whose stored timestamp falls within the
    inclusive ``[since, until]`` range. ``since``/``until`` may be date-only.
    Malformed or truncated lines are skipped."""
    path = _snapshot_path(repo_root)
    if not path.exists():
        return []

    low = _to_naive_utc(since)
    high = _to_naive_utc(until)

    out: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
                stamp = _to_naive_utc(entry["ts"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if low <= stamp <= high:
                out.append(entry.get("record", {}))
    return out
