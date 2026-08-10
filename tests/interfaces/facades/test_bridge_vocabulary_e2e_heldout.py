"""Held-out subprocess oracle for bridge vocabulary compatibility."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key in ("JIRA_URL", "JIRA_USER", "JIRA_PROJECT", "JIRA_API_TOKEN"):
        env.pop(key, None)
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_fsck_spellings_are_byte_for_byte_equivalent(rebar_repo: Path, tmp_path: Path) -> None:
    """A real empty tracker produces the same audit result through either spelling."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    option = f"--tickets-tracker={tracker}"

    canonical = _run(rebar_repo, "bridge", "fsck", option, "--output", "json")
    legacy = _run(rebar_repo, "bridge-fsck", option, "--output", "json")

    assert canonical.returncode == legacy.returncode
    assert canonical.stdout == legacy.stdout
    assert canonical.stderr == legacy.stderr
    assert '"binding_drift"' in canonical.stdout


def test_check_access_spellings_share_missing_credential_failure(rebar_repo: Path) -> None:
    """The real probe boundary preserves failure streams and status for old callers."""
    canonical = _run(rebar_repo, "bridge", "check-access")
    legacy = _run(rebar_repo, "bridge-probe")

    assert canonical.returncode == legacy.returncode != 0
    assert canonical.stdout == legacy.stdout
    assert canonical.stderr == legacy.stderr


def test_canonical_help_documents_distinct_mutating_access_check(rebar_repo: Path) -> None:
    """Primary help names all commands and warns that the access check mutates Jira."""
    group = _run(rebar_repo, "bridge", "--help")
    access = _run(rebar_repo, "bridge", "check-access", "--help")
    fsck = _run(rebar_repo, "bridge", "fsck", "--help")

    assert group.returncode == access.returncode == fsck.returncode == 0
    for command in ("fsck", "check-access", "setup"):
        assert command in group.stdout
    assert "create" in access.stdout.lower()
    assert "delete" in access.stdout.lower()
    assert "check-access" not in fsck.stdout
