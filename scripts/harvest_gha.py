#!/usr/bin/env python
"""Harvest GitHub-Actions coverage / CI-health and persist them as snapshots.

This is the persistence path for the isolated GHA adapter (ticket 1f77). It
fetches recent workflow runs via ``gh`` / the Actions API, parses coverage and
CI-health signals through ``rebar.metrics.adapters.github_actions``, and appends
each derived record to the tracked ``.rebar/metrics-snapshots.ndjson`` snapshot
store via ``rebar.metrics.snapshot.write_snapshot``.

The core ``rebar.metrics`` package does **not** import this script or the
adapter; the CI workflow (``.github/workflows/test.yml``) is not edited — this
script is the only writer of these snapshots.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from typing import Any

from rebar.metrics import snapshot
from rebar.metrics.adapters import github_actions


def persist_snapshots(repo_root: str, records: list[dict[str, Any]]) -> int:
    """Persist each record as a metrics snapshot; return the number written.

    Each record (e.g. ``{"coverage_pct": 84.2, "ts": "..."}``) is written via
    ``rebar.metrics.snapshot.write_snapshot`` using its own ``ts`` as the
    snapshot timestamp, appending to the tracked
    ``<repo_root>/.rebar/metrics-snapshots.ndjson``.
    """
    count = 0
    for record in records:
        snapshot.write_snapshot(record, repo_root=repo_root, ts=record["ts"])
        count += 1
    return count


def _fetch_runs(repo: str, limit: int) -> list[dict[str, Any]]:
    """Fetch recent workflow runs (oldest-first) via ``gh``."""
    result = subprocess.run(
        [
            "gh",
            "run",
            "list",
            "--repo",
            repo,
            "--limit",
            str(limit),
            "--json",
            "conclusion,createdAt,headSha,databaseId",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    raw = json.loads(result.stdout)
    runs = [
        {
            "conclusion": r.get("conclusion"),
            "created_at": int(datetime.fromisoformat(r["createdAt"]).timestamp()),
            "head_sha": r.get("headSha"),
        }
        for r in raw
    ]
    runs.sort(key=lambda r: r["created_at"])
    return runs


def _fetch_coverage_log(repo: str, run_id: int) -> str:
    """Fetch a run's job log text via ``gh`` (for coverage parsing)."""
    result = subprocess.run(
        ["gh", "run", "view", str(run_id), "--repo", repo, "--log"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name of the GitHub repo")
    parser.add_argument("--repo-root", default=".", help="path to the rebar checkout")
    parser.add_argument("--limit", type=int, default=50, help="how many runs to scan")
    args = parser.parse_args()

    runs = _fetch_runs(args.repo, args.limit)
    ts = datetime.now(timezone.utc).isoformat()

    records: list[dict[str, Any]] = []
    recovery = github_actions.red_to_green_recovery(runs)
    if recovery is not None:
        records.append({"red_to_green_recovery": recovery, "ts": ts})

    if runs:
        latest = max(runs, key=lambda r: r["created_at"])
        log = _fetch_coverage_log(args.repo, latest.get("databaseId", 0))
        coverage = github_actions.parse_coverage(log)
        if coverage is not None:
            records.append({"coverage_pct": coverage, "ts": ts})

    written = persist_snapshots(args.repo_root, records)
    print(f"persisted {written} snapshot(s)")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
