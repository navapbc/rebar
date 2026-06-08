"""Unit tests for audit_dd3_mutation_caps.py — per-pass mutation cap verifier.

Module loading
--------------
Loaded via importlib (worktree convention — see tests/unit/dso_reconciler/conftest.py).

Gate stub
---------
The subprocess call to audit_dd4_phase_gate.sh is monkey-patched in all tests
so the gate always passes (returncode=0). The goal of these tests is the cap
logic, not the phase-gate plumbing.

Pass-log format
---------------
One JSON object per line (JSON Lines). Required fields per record:
    phase         (str)
    pass_index    (int)  — 1-based
    mutation_count (int)
    timestamp     (str)  — ISO-8601
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = (
    REPO_ROOT
    / "src"
    / "rebar"
    / "_engine"
    / "dso_reconciler"
    / "audits"
    / "audit_dd3_mutation_caps.py"
)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("audit_dd3_mutation_caps", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def mod() -> ModuleType:
    return _load_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_pass_log(path: Path, records: list[dict]) -> None:
    """Write a JSON-Lines pass-log fixture to *path*."""
    lines = [json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n")


def _make_record(pass_index: int, mutation_count: int, phase: str = "bootstrap-strict") -> dict:
    return {
        "phase": phase,
        "pass_index": pass_index,
        "mutation_count": mutation_count,
        "timestamp": "2026-05-24T10:00:00Z",
    }


def _gate_stub(returncode: int = 0):
    """Return a mock for subprocess.run that simulates the gate returning *returncode*."""
    mock_result = subprocess.CompletedProcess(args=[], returncode=returncode)

    def _stub(cmd, **kwargs):
        # Only stub calls that look like audit_dd4_phase_gate.sh; pass everything
        # else (e.g. git rev-parse) through.
        if cmd and "audit_dd4_phase_gate.sh" in str(cmd[0]):
            return mock_result
        return subprocess.run.__wrapped__(cmd, **kwargs) if hasattr(subprocess.run, "__wrapped__") else mock_result

    return _stub


# ---------------------------------------------------------------------------
# test_all_within_cap
# ---------------------------------------------------------------------------

def test_all_within_cap(mod: ModuleType, tmp_path: Path) -> None:
    """All 8 passes within cap → exit 0, overall_pass=true."""
    records = [
        # Passes 1–5 (strict, cap=10) — all at 5 mutations
        *[_make_record(n, 5, "bootstrap-strict") for n in range(1, 6)],
        # Passes 6–8 (throttle, cap=100) — all at 50 mutations
        *[_make_record(n, 50, "bootstrap-throttle") for n in range(6, 9)],
    ]
    pass_log = tmp_path / "pass_log.jsonl"
    _write_pass_log(pass_log, records)
    artifacts_dir = tmp_path / "artifacts"

    gate_result = subprocess.CompletedProcess(args=[], returncode=0)

    with patch.object(mod.subprocess, "run", return_value=gate_result):
        # Override: only gate calls should be stubbed; git rev-parse for
        # artifacts default is not exercised when we pass --artifacts-dir.
        rc = mod.main([
            "--pass-log", str(pass_log),
            "--phase", "bootstrap-strict",
            "--artifacts-dir", str(artifacts_dir),
        ])

    assert rc == 0, f"Expected exit 0, got {rc}"

    dd3_path = artifacts_dir / "bootstrap-strict" / "dd3.json"
    assert dd3_path.exists(), "dd3.json artifact not written"
    payload = json.loads(dd3_path.read_text())
    assert payload["overall_pass"] is True
    assert len(payload["passes"]) == 8
    for entry in payload["passes"]:
        assert entry["within_cap"] is True


# ---------------------------------------------------------------------------
# test_throttle_over_cap
# ---------------------------------------------------------------------------

def test_throttle_over_cap(mod: ModuleType, tmp_path: Path) -> None:
    """Pass 7 reports 150 mutations (>100 throttle cap) → exit 5, overall_pass=false."""
    records = [
        *[_make_record(n, 5, "bootstrap-strict") for n in range(1, 6)],
        _make_record(6, 50, "bootstrap-throttle"),
        _make_record(7, 150, "bootstrap-throttle"),   # exceeds cap=100
        _make_record(8, 50, "bootstrap-throttle"),
    ]
    pass_log = tmp_path / "pass_log.jsonl"
    _write_pass_log(pass_log, records)
    artifacts_dir = tmp_path / "artifacts"

    gate_result = subprocess.CompletedProcess(args=[], returncode=0)

    with patch.object(mod.subprocess, "run", return_value=gate_result):
        rc = mod.main([
            "--pass-log", str(pass_log),
            "--phase", "bootstrap-throttle",
            "--artifacts-dir", str(artifacts_dir),
        ])

    assert rc == 5, f"Expected exit 5, got {rc}"

    dd3_path = artifacts_dir / "bootstrap-throttle" / "dd3.json"
    assert dd3_path.exists()
    payload = json.loads(dd3_path.read_text())
    assert payload["overall_pass"] is False

    pass_7 = next(e for e in payload["passes"] if e["n"] == 7)
    assert pass_7["within_cap"] is False
    assert pass_7["count"] == 150
    assert pass_7["cap"] == 100


# ---------------------------------------------------------------------------
# test_strict_over_cap
# ---------------------------------------------------------------------------

def test_strict_over_cap(mod: ModuleType, tmp_path: Path) -> None:
    """Pass 3 reports 50 mutations (>10 strict cap) → exit 5, overall_pass=false."""
    records = [
        _make_record(1, 5, "bootstrap-strict"),
        _make_record(2, 5, "bootstrap-strict"),
        _make_record(3, 50, "bootstrap-strict"),  # exceeds cap=10
        _make_record(4, 5, "bootstrap-strict"),
        _make_record(5, 5, "bootstrap-strict"),
    ]
    pass_log = tmp_path / "pass_log.jsonl"
    _write_pass_log(pass_log, records)
    artifacts_dir = tmp_path / "artifacts"

    gate_result = subprocess.CompletedProcess(args=[], returncode=0)

    with patch.object(mod.subprocess, "run", return_value=gate_result):
        rc = mod.main([
            "--pass-log", str(pass_log),
            "--phase", "bootstrap-strict",
            "--artifacts-dir", str(artifacts_dir),
        ])

    assert rc == 5, f"Expected exit 5, got {rc}"

    dd3_path = artifacts_dir / "bootstrap-strict" / "dd3.json"
    assert dd3_path.exists()
    payload = json.loads(dd3_path.read_text())
    assert payload["overall_pass"] is False

    pass_3 = next(e for e in payload["passes"] if e["n"] == 3)
    assert pass_3["within_cap"] is False
    assert pass_3["count"] == 50
    assert pass_3["cap"] == 10
