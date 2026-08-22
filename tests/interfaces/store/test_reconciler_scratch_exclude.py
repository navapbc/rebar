"""The Jira reconciler must never enumerate ``.tickets-tracker/.scratch/``.

In-process port of tests/test-reconciler-scratch-exclude.sh (the bash harness is
being deleted; the reconciler itself stays). ``.scratch/`` holds agent planning
data, not ticket events, so ``__main__ --dry-run-enumerate`` must skip it.

  A. ``--dry-run-enumerate`` output excludes any ``.scratch`` path.
  B. ``--dry-run-enumerate`` output includes the valid (non-scratch) ticket dir.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from rebar._engine import engine_env

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
        json.dumps(
            {"event_type": "CREATE", "data": {"ticket_type": "task", "title": "Fixture ticket"}}
        )
    )
    (scratch / "plan.json").write_text(
        json.dumps({"scratch": True, "note": "agent planning data — not a ticket event"})
    )
    return tmp_path


def _dry_run_enumerate(root: Path) -> str:
    cp = subprocess.run(
        [sys.executable, "-m", "rebar_reconciler", "--repo-root", str(root), "--dry-run-enumerate"],
        env=engine_env(str(root)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert cp.returncode == 0, f"--dry-run-enumerate failed: {cp.stderr}"
    return cp.stdout


def test_dry_run_enumerate_excludes_scratch(fixture_root: Path) -> None:
    assert ".scratch" not in _dry_run_enumerate(fixture_root)


def test_dry_run_enumerate_includes_valid_ticket(fixture_root: Path) -> None:
    assert _VALID_ID in _dry_run_enumerate(fixture_root)
