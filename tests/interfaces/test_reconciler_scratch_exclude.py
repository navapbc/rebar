"""The Jira reconciler must never enumerate ``.tickets-tracker/.scratch/``.

In-process port of tests/test-reconciler-scratch-exclude.sh (the bash harness is
being deleted; the reconciler itself stays). ``.scratch/`` holds agent planning
data, not ticket events — the payload-builder (``health.py`` iterdir walkers +
``__main__ --dry-run-enumerate``) must skip it.

  A. ``--dry-run-enumerate`` output excludes any ``.scratch`` path.
  B. ``--dry-run-enumerate`` output includes the valid (non-scratch) ticket dir.
  C. ``health.count_open_by_type()`` excludes the scratch subdirectory.
  D. ``health.capture_baseline()`` excludes scratch from the pre-pass total.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from rebar._engine import engine_dir, engine_env

_VALID_ID = "aaaa-bbbb-cccc-dddd"


@pytest.fixture
def fixture_root(tmp_path: Path) -> Path:
    """A minimal tracker: one real ticket dir + a .scratch/ planning dir."""
    tracker = tmp_path / ".tickets-tracker"
    valid = tracker / _VALID_ID
    scratch = tracker / ".scratch" / _VALID_ID
    valid.mkdir(parents=True)
    scratch.mkdir(parents=True)
    (valid / "1000000000-create.json").write_text(
        json.dumps({"event_type": "CREATE", "data": {"ticket_type": "task", "title": "Fixture ticket"}})
    )
    (scratch / "plan.json").write_text(
        json.dumps({"scratch": True, "note": "agent planning data — not a ticket event"})
    )
    return tmp_path


def _load_health():
    """Spec-load health.py standalone (it is stdlib-only — no sibling imports)."""
    path = engine_dir() / "rebar_reconciler" / "health.py"
    spec = importlib.util.spec_from_file_location("_health_scratch_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dry_run_enumerate(root: Path) -> str:
    cp = subprocess.run(
        [sys.executable, "-m", "rebar_reconciler", "--repo-root", str(root), "--dry-run-enumerate"],
        env=engine_env(str(root)), capture_output=True, text=True,
    )
    assert cp.returncode == 0, f"--dry-run-enumerate failed: {cp.stderr}"
    return cp.stdout


def test_dry_run_enumerate_excludes_scratch(fixture_root: Path) -> None:
    assert ".scratch" not in _dry_run_enumerate(fixture_root)


def test_dry_run_enumerate_includes_valid_ticket(fixture_root: Path) -> None:
    assert _VALID_ID in _dry_run_enumerate(fixture_root)


def test_count_open_by_type_excludes_scratch(fixture_root: Path) -> None:
    health = _load_health()
    counts = health.count_open_by_type(repo_root=fixture_root)
    assert counts.get("task") == 1  # exactly the one valid open ticket
    assert ".scratch" not in counts


def test_capture_baseline_excludes_scratch(fixture_root: Path) -> None:
    health = _load_health()
    baseline_path = health.capture_baseline("test-pass-001", repo_root=fixture_root)
    baseline = json.loads(Path(baseline_path).read_text())
    assert baseline["pre_pass_fsck_total"] == 1  # scratch dir not counted
