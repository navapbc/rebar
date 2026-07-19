"""Held-out contracts for the metrics snapshot store (ticket 3275). WITHHELD.

- append accumulates (the series grows; a second write does not clobber the first),
- a malformed/truncated NDJSON line is skipped without raising (reader returns the
  valid records),
- the store path is ``.rebar/metrics-snapshots.ndjson`` and is NOT matched by the
  project's real ``.gitignore`` (whereas a ``reports/`` path WOULD be) — the T1 fix
  that makes the going-forward series actually persist.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar.metrics.snapshot import read_snapshots, write_snapshot

pytestmark = pytest.mark.unit

SNAPSHOT_PATH = ".rebar/metrics-snapshots.ndjson"


def _repo_gitignore() -> str:
    """The project's real .gitignore text (walk up from this test file)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        gi = parent / ".gitignore"
        if gi.exists() and (parent / "src" / "rebar").exists():
            return gi.read_text(encoding="utf-8")
    raise AssertionError("could not locate the project .gitignore")


def test_append_accumulates(tmp_path):
    repo = str(tmp_path)
    write_snapshot({"n": 1}, repo_root=repo, ts="2026-02-01T00:00:00+00:00")
    write_snapshot({"n": 2}, repo_root=repo, ts="2026-02-02T00:00:00+00:00")

    got = read_snapshots("2026-01-01", "2026-03-01", repo_root=repo)
    ns = sorted(r.get("n") for r in got if "n" in r)
    assert ns == [1, 2], "append must accumulate both records, not clobber"


def test_malformed_line_skipped(tmp_path):
    repo = str(tmp_path)
    write_snapshot({"n": 1}, repo_root=repo, ts="2026-02-01T00:00:00+00:00")
    # Corrupt the store with a truncated/garbage line.
    store = tmp_path / SNAPSHOT_PATH
    with store.open("a", encoding="utf-8") as fh:
        fh.write("{not valid json at all\n")
    write_snapshot({"n": 2}, repo_root=repo, ts="2026-02-02T00:00:00+00:00")

    got = read_snapshots("2026-01-01", "2026-03-01", repo_root=repo)  # must not raise
    ns = sorted(r.get("n") for r in got if "n" in r)
    assert ns == [1, 2], "reader must skip the malformed line and return the valid records"


def test_snapshot_path_is_not_gitignored(tmp_path):
    # Faithful check of the T1 fix: in a repo carrying the project's real .gitignore,
    # the .rebar snapshot path is tracked while a reports/ path would be ignored.
    repo = tmp_path
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    (repo / ".gitignore").write_text(_repo_gitignore(), encoding="utf-8")
    (repo / ".rebar").mkdir()
    (repo / ".rebar" / "metrics-snapshots.ndjson").write_text("{}\n", encoding="utf-8")
    (repo / "reports").mkdir()
    (repo / "reports" / "metrics-snapshots.ndjson").write_text("{}\n", encoding="utf-8")

    def ignored(rel: str) -> bool:
        return (
            subprocess.run(["git", "check-ignore", rel], cwd=repo, capture_output=True).returncode
            == 0
        )

    assert not ignored(SNAPSHOT_PATH), (
        ".rebar snapshot path must NOT be gitignored (it must persist)"
    )
    assert ignored("reports/metrics-snapshots.ndjson"), (
        "reports/ IS gitignored — the reason the store moved to .rebar"
    )
