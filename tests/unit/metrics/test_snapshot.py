"""Contract tests for the metrics snapshot store.

``write_snapshot`` appends timestamped records to ``.rebar/metrics-snapshots.ndjson``.
``read_snapshots`` returns records whose timestamps fall within the inclusive requested range.
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
