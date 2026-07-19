"""Held-out contracts for the GHA adapter (ticket 1f77). WITHHELD.

- the harvest script persists coverage to the TRACKED .rebar snapshot store (not reports/),
- the core metrics modules do NOT import the GHA adapter (portability guard),
- a no-recovery run set (still failing) yields None.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from rebar.metrics.adapters.github_actions import red_to_green_recovery
from rebar.metrics.snapshot import read_snapshots

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]
_HARVEST = _ROOT / "scripts" / "harvest_gha.py"


def _load_harvest():
    spec = importlib.util.spec_from_file_location("harvest_gha", _HARVEST)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_no_passing_run_yields_none():
    runs = [
        {"conclusion": "failure", "created_at": 1000, "head_sha": "a"},
        {"conclusion": "failure", "created_at": 1600, "head_sha": "a"},
    ]
    assert red_to_green_recovery(runs) is None


def test_harvest_persists_coverage_to_rebar_store(tmp_path, monkeypatch):
    # The harvester writes harvested coverage as a snapshot to .rebar (the tracked store).
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    import rebar

    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))

    mod = _load_harvest()
    # Feed the harvester fixture run data (coverage injected) and have it persist.
    # The script exposes `persist_snapshots(repo_root, records)` via snapshot.write_snapshot.
    mod.persist_snapshots(str(repo), [{"coverage_pct": 84.2, "ts": "2026-03-15T00:00:00+00:00"}])

    recs = read_snapshots("2026-01-01", "2026-12-31", repo_root=str(repo))
    assert any(r.get("coverage_pct") == 84.2 for r in recs), (
        "harvester must persist to the .rebar store"
    )


def test_core_does_not_import_gha_adapter():
    # Portability guard: the core metrics modules must not import the optional adapter.
    for rel in ("src/rebar/metrics/registry.py", "src/rebar/metrics/__init__.py"):
        src = (_ROOT / rel).read_text(encoding="utf-8")
        assert "adapters.github_actions" not in src, f"{rel} must not import the GHA adapter"
        assert "adapters import github_actions" not in src
