"""Happy-path contract for the going-forward metrics snapshot store (ticket 3275).

Tier: unit (real temp dir). Pins the core round-trip: a snapshot written with a
timestamp is read back when the query range includes that timestamp, and excluded
when it does not. Malformed-line tolerance and the tracked-path (non-gitignored)
contract live in the held-out companion.

Public surface (from ``rebar.metrics.snapshot``):
- ``write_snapshot(record: dict, *, repo_root, ts: str)`` — append one record,
  tagged with ISO-8601 ``ts``, to ``<repo_root>/.rebar/metrics-snapshots.ndjson``.
- ``read_snapshots(since: str, until: str, *, repo_root) -> list[dict]`` — records
  whose timestamp falls within [since, until].
"""

from __future__ import annotations

import pytest

from rebar.metrics.snapshot import read_snapshots, write_snapshot

pytestmark = pytest.mark.unit


def test_snapshot_round_trips_within_range(tmp_path):
    repo = str(tmp_path)
    rec = {"coverage_pct": 91.2, "clone_count": 7}
    write_snapshot(rec, repo_root=repo, ts="2026-03-15T00:00:00+00:00")

    got = read_snapshots("2026-01-01", "2026-06-01", repo_root=repo)
    assert any(r.get("coverage_pct") == 91.2 and r.get("clone_count") == 7 for r in got)


def test_snapshot_excluded_outside_range(tmp_path):
    repo = str(tmp_path)
    write_snapshot({"coverage_pct": 80.0}, repo_root=repo, ts="2026-03-15T00:00:00+00:00")

    # A range entirely after the snapshot must not return it.
    got = read_snapshots("2026-06-01", "2026-12-01", repo_root=repo)
    assert all(r.get("coverage_pct") != 80.0 for r in got)
