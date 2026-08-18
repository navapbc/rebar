"""Held-out subprocess oracle for bridge vocabulary compatibility."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.integration


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = subprocess_env()
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
    """A real committed tracker produces the new result through either spelling."""
    tracker = rebar_repo / ".tickets-tracker"
    option = f"--tickets-tracker={tracker}"

    canonical = _run(rebar_repo, "bridge", "fsck", option, "--output", "json")
    legacy = _run(rebar_repo, "bridge-fsck", option, "--output", "json")

    assert canonical.returncode == legacy.returncode
    assert canonical.stdout == legacy.stdout
    assert canonical.stderr == legacy.stderr
    assert canonical.returncode == 0
    assert set(json.loads(canonical.stdout)) == {
        "unknown_event_types",
        "binding_drift",
        "store_integrity",
    }


def test_fsck_spellings_share_operational_failure_exit_two(
    rebar_repo: Path, tmp_path: Path
) -> None:
    """Old workflows receive the same fail-closed behavior as the canonical command."""
    tracker = tmp_path / "not-a-store"
    tracker.mkdir()
    option = f"--tickets-tracker={tracker}"

    canonical = _run(rebar_repo, "bridge", "fsck", option, "--output", "json")
    legacy = _run(rebar_repo, "bridge-fsck", option, "--output", "json")

    assert canonical.returncode == legacy.returncode == 2
    assert canonical.stdout == legacy.stdout == ""
    assert canonical.stderr == legacy.stderr
    assert "tickets" in canonical.stderr.lower() or "git" in canonical.stderr.lower()


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
    assert "store integrity" in fsck.stdout.lower()
    for phantom in ("orphan", "duplicate", "stale sync"):
        assert phantom not in fsck.stdout.lower()
