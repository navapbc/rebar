"""RED tests for the 'ticket bridge-status' command.

These tests are RED — they define the expected behavior for 'ticket bridge-status'
before the implementation exists. All test functions must FAIL before
ticket-bridge-status.sh is implemented and the dispatcher is updated.

The command is:
    ticket bridge-status [--output json]

Status file: $(git rev-parse --show-toplevel)/.tickets-tracker/.bridge-status.json
Format:
    {
        "last_run_timestamp": int (UTC epoch),
        "success": bool,
        "error": str | null,
        "unresolved_conflicts": int
    }

Behaviors:
- No status file → exit non-zero, print message to stderr
- Status file present, success=true → show last_run_timestamp, status success
- Status file present, success=false → show failure reason
- Status file present, unresolved_conflicts > 0 → show conflict count
- --output json → output valid JSON with required keys

Test: python3 -m pytest tests/scripts/test_bridge_status.py
All tests must return non-zero until ticket-bridge-status.sh is implemented.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(
    subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
)
TICKET_CMD = REPO_ROOT / "src" / "rebar" / "_engine" / "ticket"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LAST_RUN_TS = 1742700000  # fixed epoch for fixture assertions


def _run_bridge_status(
    tracker_dir: Path,
    extra_args: list[str] | None = None,
    *,
    env_override: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Invoke 'ticket bridge-status' with the given tracker dir and optional args.

    Passes TICKETS_TRACKER_DIR so the script reads from tmp_path rather than the
    real repository's .tickets-tracker directory.
    """
    env = {
        **os.environ,
        "TICKETS_TRACKER_DIR": str(tracker_dir),
        # Prevent the dispatcher's _ensure_initialized from invoking ticket-init.sh
        # by pointing GIT_DIR at an existing directory. The actual git repo works fine.
        "GIT_DIR": str(REPO_ROOT / ".git"),
    }
    if env_override:
        env.update(env_override)

    cmd = ["bash", str(TICKET_CMD), "bridge-status"] + (extra_args or [])
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=15,
    )


def _write_status_file(tracker_dir: Path, payload: dict) -> Path:
    """Write .bridge-status.json into tracker_dir and return its path."""
    status_path = tracker_dir / ".bridge-status.json"
    status_path.write_text(json.dumps(payload))
    return status_path


# ---------------------------------------------------------------------------
# Test 1: last run time shown when status file is present and run succeeded
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_bridge_status_shows_last_run_time(tmp_path: Path) -> None:
    """Given a .bridge-status.json with last_run_timestamp and success=true,
    'ticket bridge-status' must print the timestamp value in its output.

    The timestamp is an integer UTC epoch. The output need not format it as
    a human-readable date — including the raw integer is sufficient.
    """
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()

    _write_status_file(
        tracker_dir,
        {
            "last_run_timestamp": _LAST_RUN_TS,
            "success": True,
            "error": None,
            "unresolved_conflicts": 0,
        },
    )

    result = _run_bridge_status(tracker_dir)

    assert result.returncode == 0, (
        f"bridge-status must exit 0 when status file is present and success=true.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    combined_output = result.stdout + result.stderr
    assert str(_LAST_RUN_TS) in combined_output, (
        f"Output must contain last_run_timestamp value '{_LAST_RUN_TS}'.\n"
        f"Got stdout: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: failure shown when last run failed
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_bridge_status_shows_failure_when_last_run_failed(tmp_path: Path) -> None:
    """Given a .bridge-status.json with success=false and error='auth_failure',
    'ticket bridge-status' must include 'failure' or 'failed' in its output
    AND include the error reason 'auth_failure'.
    """
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()

    _write_status_file(
        tracker_dir,
        {
            "last_run_timestamp": _LAST_RUN_TS,
            "success": False,
            "error": "auth_failure",
            "unresolved_conflicts": 0,
        },
    )

    result = _run_bridge_status(tracker_dir)

    # Exit code may be 0 or non-zero for a failed run — not specified; output is the contract
    combined_output = (result.stdout + result.stderr).lower()
    assert "fail" in combined_output, (
        f"Output must contain 'failure' or 'failed' when success=false.\n"
        f"Got stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    assert "auth_failure" in (result.stdout + result.stderr), (
        f"Output must contain the error reason 'auth_failure'.\n"
        f"Got stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: unresolved conflict count shown when > 0
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_bridge_status_shows_unresolved_conflicts(tmp_path: Path) -> None:
    """Given a .bridge-status.json with unresolved_conflicts=3,
    'ticket bridge-status' must include the count '3' in its output.
    """
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()

    _write_status_file(
        tracker_dir,
        {
            "last_run_timestamp": _LAST_RUN_TS,
            "success": True,
            "error": None,
            "unresolved_conflicts": 3,
        },
    )

    result = _run_bridge_status(tracker_dir)

    assert result.returncode == 0, (
        f"bridge-status must exit 0 when status file is present.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    combined_output = result.stdout + result.stderr
    assert "3" in combined_output, (
        f"Output must contain unresolved_conflicts count '3'.\n"
        f"Got stdout: {result.stdout!r}"
    )
    # Verify the output relates the number to conflicts, not a random '3'
    lower_output = combined_output.lower()
    assert "conflict" in lower_output or "unresolved" in lower_output, (
        f"Output must contain 'conflict' or 'unresolved' alongside the count.\n"
        f"Got stdout: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: exits non-zero when no status file exists
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_bridge_status_exits_nonzero_when_no_status_file(tmp_path: Path) -> None:
    """When .bridge-status.json is absent, 'ticket bridge-status' must either:
      (a) exit non-zero, OR
      (b) print a message indicating no status file is found.

    Both behaviours satisfy the contract; at least one must hold.
    """
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()
    # Deliberately do NOT write .bridge-status.json

    result = _run_bridge_status(tracker_dir)

    combined_output = (result.stdout + result.stderr).lower()
    no_file_message = (
        "no bridge status" in combined_output
        or "not found" in combined_output
        or "has the bridge run" in combined_output
        or "no status" in combined_output
    )
    exits_nonzero = result.returncode != 0

    assert exits_nonzero or no_file_message, (
        f"When .bridge-status.json is missing, bridge-status must exit non-zero "
        f"OR print a 'no status file' message.\n"
        f"returncode={result.returncode}\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: --output json outputs valid JSON with required keys
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_bridge_status_json_output_format(tmp_path: Path) -> None:
    """Given a valid .bridge-status.json, 'ticket bridge-status --output json'
    must output valid JSON containing the keys:
        last_run_timestamp, success, error, unresolved_conflicts

    The output may contain additional keys (e.g., computed unresolved_alerts_count).
    """
    tracker_dir = tmp_path / ".tickets-tracker"
    tracker_dir.mkdir()

    _write_status_file(
        tracker_dir,
        {
            "last_run_timestamp": _LAST_RUN_TS,
            "success": True,
            "error": None,
            "unresolved_conflicts": 0,
        },
    )

    result = _run_bridge_status(tracker_dir, extra_args=["--output", "json"])

    assert result.returncode == 0, (
        f"bridge-status --output json must exit 0 when status file is present.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )

    try:
        data = json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"bridge-status --output json output is not valid JSON: {exc}\n"
            f"stdout: {result.stdout!r}"
        )

    required_keys = {"last_run_timestamp", "success", "error", "unresolved_conflicts"}
    missing = required_keys - set(data.keys())
    assert not missing, (
        f"JSON output is missing required keys: {missing}\nGot keys: {set(data.keys())}"
    )

    assert data["last_run_timestamp"] == _LAST_RUN_TS, (
        f"last_run_timestamp in JSON output must match fixture value {_LAST_RUN_TS}.\n"
        f"Got: {data['last_run_timestamp']!r}"
    )
    assert data["success"] is True, (
        f"success in JSON output must be true (bool).\nGot: {data['success']!r}"
    )
