"""RED tests for ticket-bridge-fsck.py bridge mapping audit command.

These tests are RED — they test functionality that does not yet exist.
All test functions must FAIL before ticket-bridge-fsck.py is implemented
and 'bridge-fsck' is registered in the ticket dispatcher.

This is a BRIDGE-SPECIFIC fsck (mapping audit, orphans, duplicates, stale
SYNC events) — distinct from the existing ticket-fsck.sh (JSON validity +
CREATE presence + index.lock cleanup).

The bridge-fsck command is expected to expose a Python module interface:
    audit_bridge_mappings(tickets_tracker: Path) -> dict
        Scan all ticket directories under tickets_tracker, read SYNC event
        files, and return a findings dict with keys:
          - 'orphaned': list of dicts {ticket_id, jira_key} for tickets that
            have a SYNC event mapping to a jira_key but no corresponding
            local ticket directory entry with a valid CREATE event.
          - 'duplicates': list of dicts {jira_key, ticket_ids} for jira_keys
            that are mapped to more than one local ticket_id via SYNC events.
          - 'stale': list of dicts {ticket_id, jira_key, last_sync_ts} for
            tickets whose most recent SYNC event is >30 days old with no
            subsequent BRIDGE_ALERT activity.

        Findings dict values are empty lists when no issues are found.

CLI behavior (invoked as 'ticket bridge-fsck'):
    - Exit 0 when no issues found; outputs a "no issues found" message.
    - Exit non-zero (e.g. 1) when any issue category is non-empty.
    - Outputs the jira_key for each orphaned mapping (contains 'orphan').
    - Outputs the jira_key for each duplicate mapping (contains 'duplicate').
    - Outputs 'stale' or 'stale_sync' for stale SYNC events.

SYNC event format (flat, from sync-event-format.md contract):
    {
        "event_type": "SYNC",
        "jira_key": str,       -- e.g. "DSO-99"
        "local_id": str,       -- local ticket ID
        "env_id": str,         -- UUID4 bridge env identity
        "timestamp": int,      -- UTC epoch seconds
        "run_id": str          -- GHA run ID (may be empty string)
    }
    Note: SYNC events are flat (no 'uuid'/'author'/'data' wrapper) per
    sync-event-format.md; BRIDGE_ALERT events use the standard event base
    schema with uuid/env_id/timestamp/data fields.

Test: python3 -m pytest tests/scripts/test_bridge_fsck.py -v
All tests must return non-zero until ticket-bridge-fsck.py is implemented.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading — filename has hyphens so we use importlib
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "ticket-bridge-fsck.py"
TICKET_DISPATCHER = REPO_ROOT / "src" / "rebar" / "_engine" / "ticket"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ticket_bridge_fsck", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture(scope="module")
def fsck() -> ModuleType:
    """Return the ticket-bridge-fsck module, failing all tests if absent (RED)."""
    if not SCRIPT_PATH.exists():
        pytest.fail(
            f"ticket-bridge-fsck.py not found at {SCRIPT_PATH} — "
            "this is expected RED state; implement the script to make tests pass."
        )
    return _load_module()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BRIDGE_ENV_ID = "bbbbbbbb-0000-4000-8000-000000000002"
_OTHER_ENV_ID = "aaaaaaaa-0000-4000-8000-000000000001"

# Fixed UUIDs for deterministic test files
_UUID_CREATE = "11111111-1111-4111-8111-111111111111"
_UUID_SYNC_1 = "22222222-2222-4222-8222-222222222222"
_UUID_SYNC_2 = "33333333-3333-4333-8333-333333333333"
_UUID_SYNC_3 = "44444444-4444-4444-8444-444444444444"
_UUID_ALERT = "55555555-5555-4555-8555-555555555555"

# A "now" base timestamp in the past so stale tests can use >30 days ago (nanoseconds)
_NOW_TS = 1742605200 * 1_000_000_000  # 2026-03-21T12:00:00Z in nanoseconds
_STALE_TS = _NOW_TS - (31 * 24 * 3600 * 1_000_000_000)  # 31 days ago in nanoseconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_create_event(
    ticket_dir: Path,
    timestamp: int,
    uuid: str = _UUID_CREATE,
    title: str = "Test ticket",
    env_id: str = _OTHER_ENV_ID,
) -> Path:
    """Write a standard CREATE event file and return its path."""
    filename = f"{timestamp}-{uuid}-CREATE.json"
    payload = {
        "event_type": "CREATE",
        "uuid": uuid,
        "timestamp": timestamp,
        "author": "test-author",
        "env_id": env_id,
        "data": {
            "ticket_type": "task",
            "title": title,
            "parent_id": None,
        },
    }
    path = ticket_dir / filename
    path.write_text(json.dumps(payload))
    return path


def _write_sync_event(
    ticket_dir: Path,
    jira_key: str,
    local_id: str,
    timestamp: int,
    uuid: str = _UUID_SYNC_1,
    env_id: str = _BRIDGE_ENV_ID,
    run_id: str = "12345678901",
) -> Path:
    """Write a flat SYNC event file (no uuid/author/data wrapper) and return its path."""
    filename = f"{timestamp}-{uuid}-SYNC.json"
    payload = {
        "event_type": "SYNC",
        "jira_key": jira_key,
        "local_id": local_id,
        "env_id": env_id,
        "timestamp": timestamp,
        "run_id": run_id,
    }
    path = ticket_dir / filename
    path.write_text(json.dumps(payload))
    return path


def _write_bridge_alert_event(
    ticket_dir: Path,
    timestamp: int,
    uuid: str = _UUID_ALERT,
    reason: str = "test alert",
    resolved: bool = False,
    env_id: str = _BRIDGE_ENV_ID,
) -> Path:
    """Write a BRIDGE_ALERT event file (full base schema) and return its path."""
    filename = f"{timestamp}-{uuid}-BRIDGE_ALERT.json"
    payload = {
        "event_type": "BRIDGE_ALERT",
        "uuid": uuid,
        "timestamp": timestamp,
        "author": "bridge-test",
        "env_id": env_id,
        "data": {
            "reason": reason,
            "resolved": resolved,
        },
    }
    path = ticket_dir / filename
    path.write_text(json.dumps(payload))
    return path


def _run_bridge_fsck(
    tickets_tracker: Path,
) -> subprocess.CompletedProcess[str]:
    """Invoke 'ticket bridge-fsck' via the dispatcher and return the result."""
    return subprocess.run(
        ["bash", str(TICKET_DISPATCHER), "bridge-fsck"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(REPO_ROOT),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "TICKETS_TRACKER_DIR": str(tickets_tracker),
        },
    )


# ---------------------------------------------------------------------------
# Test 1: Orphaned ticket detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_bridge_fsck_detects_orphaned_ticket(tmp_path: Path, fsck: ModuleType) -> None:
    """audit_bridge_mappings() must detect a SYNC event whose local ticket
    directory exists but has no CREATE event (orphaned mapping).

    An 'orphaned' ticket in bridge terms is a ticket directory that has a
    SYNC event (mapping it to a jira_key) but lacks a valid local CREATE
    event — meaning the ticket exists in the bridge mapping but has no
    corresponding local ticket record.

    Setup:
    - Create a ticket directory 'w21-orphan' with SYNC mapping to 'DSO-99'.
    - Do NOT write a CREATE event in that directory.
    - Call audit_bridge_mappings().

    Expected:
    - findings['orphaned'] is non-empty.
    - The orphaned entry references jira_key 'DSO-99'.
    """
    tracker = tmp_path / ".tickets-tracker"
    ticket_dir = tracker / "w21-orphan"
    ticket_dir.mkdir(parents=True)

    # Write SYNC event but NO CREATE event — this is the orphan condition
    _write_sync_event(
        ticket_dir,
        jira_key="DSO-99",
        local_id="w21-orphan",
        timestamp=_NOW_TS - 3600,
    )

    findings = fsck.audit_bridge_mappings(tracker, now_ts=_NOW_TS)

    orphaned = findings.get("orphaned", [])
    assert len(orphaned) >= 1, (
        "audit_bridge_mappings() must detect at least one orphaned mapping "
        "when a ticket has a SYNC event but no CREATE event"
    )
    jira_keys_found = [entry.get("jira_key") for entry in orphaned]
    assert "DSO-99" in jira_keys_found, (
        f"orphaned findings must include jira_key 'DSO-99'; got: {jira_keys_found}"
    )


# ---------------------------------------------------------------------------
# Test 2: Duplicate Jira key mapping detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_bridge_fsck_detects_duplicate_jira_mapping(
    tmp_path: Path, fsck: ModuleType
) -> None:
    """audit_bridge_mappings() must detect when two different local tickets
    both have SYNC events mapping to the same jira_key.

    Setup:
    - Create ticket 'w21-dup-a' with CREATE + SYNC mapping to 'DSO-77'.
    - Create ticket 'w21-dup-b' with CREATE + SYNC mapping to 'DSO-77'.
    - Call audit_bridge_mappings().

    Expected:
    - findings['duplicates'] is non-empty.
    - The duplicate entry references jira_key 'DSO-77'.
    """
    tracker = tmp_path / ".tickets-tracker"

    # Ticket A — maps to DSO-77
    ticket_dir_a = tracker / "w21-dup-a"
    ticket_dir_a.mkdir(parents=True)
    _write_create_event(ticket_dir_a, timestamp=_NOW_TS - 7200, title="Dup A")
    _write_sync_event(
        ticket_dir_a,
        jira_key="DSO-77",
        local_id="w21-dup-a",
        timestamp=_NOW_TS - 3600,
        uuid=_UUID_SYNC_1,
    )

    # Ticket B — also maps to DSO-77 (duplicate!)
    ticket_dir_b = tracker / "w21-dup-b"
    ticket_dir_b.mkdir(parents=True)
    _write_create_event(
        ticket_dir_b,
        timestamp=_NOW_TS - 7100,
        title="Dup B",
        uuid="99999999-9999-4999-8999-999999999999",
    )
    _write_sync_event(
        ticket_dir_b,
        jira_key="DSO-77",
        local_id="w21-dup-b",
        timestamp=_NOW_TS - 3500,
        uuid=_UUID_SYNC_2,
    )

    findings = fsck.audit_bridge_mappings(tracker, now_ts=_NOW_TS)

    duplicates = findings.get("duplicates", [])
    assert len(duplicates) >= 1, (
        "audit_bridge_mappings() must detect at least one duplicate mapping "
        "when two tickets share the same jira_key via SYNC events"
    )
    dup_keys = [entry.get("jira_key") for entry in duplicates]
    assert "DSO-77" in dup_keys, (
        f"duplicate findings must include jira_key 'DSO-77'; got: {dup_keys}"
    )


# ---------------------------------------------------------------------------
# Test 3: Stale SYNC event detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_bridge_fsck_detects_stale_sync_events(
    tmp_path: Path, fsck: ModuleType
) -> None:
    """audit_bridge_mappings() must detect SYNC events older than 30 days
    with no subsequent BRIDGE_ALERT activity.

    A SYNC event is 'stale' when:
    - Its timestamp is >30 days before the current time.
    - No BRIDGE_ALERT event exists in the ticket directory after the SYNC.

    Setup:
    - Create ticket 'w21-stale' with CREATE + SYNC timestamp >30 days ago.
    - Do NOT write any BRIDGE_ALERT events.
    - Call audit_bridge_mappings().

    Expected:
    - findings['stale'] is non-empty.
    - The stale entry references the ticket or its jira_key.
    """
    tracker = tmp_path / ".tickets-tracker"
    ticket_dir = tracker / "w21-stale"
    ticket_dir.mkdir(parents=True)

    _write_create_event(ticket_dir, timestamp=_STALE_TS - 3600, title="Stale ticket")
    _write_sync_event(
        ticket_dir,
        jira_key="DSO-55",
        local_id="w21-stale",
        timestamp=_STALE_TS,  # >30 days ago
        uuid=_UUID_SYNC_1,
    )
    # No BRIDGE_ALERT event — so SYNC remains stale

    findings = fsck.audit_bridge_mappings(tracker, now_ts=_NOW_TS)

    stale = findings.get("stale", [])
    assert len(stale) >= 1, (
        "audit_bridge_mappings() must detect at least one stale SYNC event "
        "when the SYNC timestamp is >30 days old with no subsequent BRIDGE_ALERT"
    )
    stale_ticket_ids = [entry.get("ticket_id") for entry in stale]
    stale_jira_keys = [entry.get("jira_key") for entry in stale]
    assert "w21-stale" in stale_ticket_ids or "DSO-55" in stale_jira_keys, (
        f"stale findings must reference ticket 'w21-stale' or jira_key 'DSO-55'; "
        f"got ticket_ids={stale_ticket_ids}, jira_keys={stale_jira_keys}"
    )


# ---------------------------------------------------------------------------
# Test 4: Clean output when no issues found
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_bridge_fsck_clean_output_when_no_issues(
    tmp_path: Path, fsck: ModuleType
) -> None:
    """audit_bridge_mappings() must return empty findings for a clean ticket
    with a valid, recent SYNC mapping.

    A 'clean' ticket has:
    - A valid CREATE event in its directory.
    - A recent SYNC event (within the last 30 days).
    - No duplicate jira_key mapping from other tickets.

    Setup:
    - Create ticket 'w21-clean' with CREATE + SYNC to 'DSO-10' (recent timestamp).
    - Call audit_bridge_mappings().

    Expected:
    - findings['orphaned'] is empty.
    - findings['duplicates'] is empty.
    - findings['stale'] is empty.
    """
    tracker = tmp_path / ".tickets-tracker"
    ticket_dir = tracker / "w21-clean"
    ticket_dir.mkdir(parents=True)

    recent_ts = _NOW_TS - (7 * 24 * 3600)  # 7 days ago — well within 30-day window
    _write_create_event(ticket_dir, timestamp=recent_ts - 3600, title="Clean ticket")
    _write_sync_event(
        ticket_dir,
        jira_key="DSO-10",
        local_id="w21-clean",
        timestamp=recent_ts,
        uuid=_UUID_SYNC_1,
    )

    findings = fsck.audit_bridge_mappings(tracker, now_ts=_NOW_TS)

    assert findings.get("orphaned", []) == [], (
        f"No orphans expected for a clean ticket; got: {findings.get('orphaned')}"
    )
    assert findings.get("duplicates", []) == [], (
        f"No duplicates expected for a clean ticket; got: {findings.get('duplicates')}"
    )
    assert findings.get("stale", []) == [], (
        f"No stale findings expected for a recent SYNC; got: {findings.get('stale')}"
    )


# ---------------------------------------------------------------------------
# Test 5: Exit code behavior
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_bridge_fsck_exit_code(tmp_path: Path, fsck: ModuleType) -> None:
    """ticket bridge-fsck must exit non-zero when issues exist, exit 0 when clean.

    This test exercises the CLI entry-point behavior by calling the module's
    main() or run() function with a patched tickets_tracker path, then
    verifying the returned/raised exit code.

    With issues (orphaned ticket):
    - Expects non-zero exit code (e.g. SystemExit(1)) or a return value != 0.

    Without issues (clean ticket):
    - Expects exit code 0 (or return value 0, or no SystemExit raised).
    """
    # --- Sub-test A: issues present → non-zero exit ---
    tracker_issues = tmp_path / "tracker-issues"

    ticket_dir_bad = tracker_issues / "w21-exit-orphan"
    ticket_dir_bad.mkdir(parents=True)
    # SYNC event but no CREATE — orphaned mapping
    _write_sync_event(
        ticket_dir_bad,
        jira_key="DSO-88",
        local_id="w21-exit-orphan",
        timestamp=_NOW_TS - 1800,
        uuid=_UUID_SYNC_3,
    )

    # The implementation must either raise SystemExit(non-zero) or return non-zero
    # Pass --now-ts so stale detection is deterministic regardless of when tests run.
    exit_code_issues: int | None = None
    try:
        result = fsck.main(
            ["--tickets-tracker", str(tracker_issues), "--now-ts", str(_NOW_TS)]
        )
        exit_code_issues = result if isinstance(result, int) else 1
    except SystemExit as exc:
        exit_code_issues = exc.code if isinstance(exc.code, int) else 1

    assert exit_code_issues != 0, (
        f"ticket bridge-fsck must exit non-zero when issues are present; "
        f"got exit code: {exit_code_issues}"
    )

    # --- Sub-test B: no issues → exit 0 ---
    tracker_clean = tmp_path / "tracker-clean"

    ticket_dir_ok = tracker_clean / "w21-exit-clean"
    ticket_dir_ok.mkdir(parents=True)
    recent_ts = _NOW_TS - (3 * 24 * 3600)  # 3 days ago
    _write_create_event(ticket_dir_ok, timestamp=recent_ts - 3600, title="Exit clean")
    _write_sync_event(
        ticket_dir_ok,
        jira_key="DSO-11",
        local_id="w21-exit-clean",
        timestamp=recent_ts,
        uuid=_UUID_SYNC_2,
    )

    exit_code_clean: int | None = None
    try:
        result = fsck.main(
            ["--tickets-tracker", str(tracker_clean), "--now-ts", str(_NOW_TS)]
        )
        exit_code_clean = result if isinstance(result, int) else 0
    except SystemExit as exc:
        exit_code_clean = exc.code if isinstance(exc.code, int) else 1

    assert exit_code_clean == 0, (
        f"ticket bridge-fsck must exit 0 when no issues are found; "
        f"got exit code: {exit_code_clean}"
    )


# ---------------------------------------------------------------------------
# Test 6: Mixed-precision BRIDGE_ALERT suppression
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_bridge_fsck_mixed_precision_alert_does_not_suppress_stale(
    tmp_path: Path, fsck: ModuleType
) -> None:
    """A nanosecond-scale BRIDGE_ALERT after a seconds-scale SYNC must not
    suppress stale detection when the alert actually occurred after the sync.

    Migration scenario: legacy SYNC events have seconds-scale timestamps (~1.7e9);
    newly written BRIDGE_ALERTs use nanoseconds (~1.7e18). The stale comparison
    must normalize both before checking ordering, otherwise any ns-scale alert
    would always satisfy alert_ts > legacy_sync_ts, hiding genuine stale tickets.
    """
    tracker = tmp_path / ".tickets-tracker"
    ticket_dir = tracker / "w21-mixed"
    ticket_dir.mkdir(parents=True)

    # Legacy SYNC: seconds-scale timestamp, >30 days before _NOW_TS
    legacy_stale_ts = 1742605200 - (31 * 24 * 3600)  # seconds-scale, stale
    _write_create_event(
        ticket_dir, timestamp=legacy_stale_ts - 3600, title="Mixed ticket"
    )
    _write_sync_event(
        ticket_dir,
        jira_key="DSO-MIXED",
        local_id="w21-mixed",
        timestamp=legacy_stale_ts,
    )

    # BRIDGE_ALERT written BEFORE the legacy sync (in real time, but ns-scale value is huge)
    # The alert occurred at legacy_stale_ts - 1000s (i.e., before the sync).
    # In nanoseconds: (legacy_stale_ts - 1000) * 1e9 — this is still BEFORE sync_ts_ns.
    alert_before_sync_ns = (legacy_stale_ts - 1000) * 1_000_000_000
    _write_bridge_alert_event(
        ticket_dir,
        timestamp=alert_before_sync_ns,
        uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab",
        reason="alert before sync",
    )

    # now_ts: nanoseconds, far in the future relative to the stale sync
    findings = fsck.audit_bridge_mappings(tracker, now_ts=_NOW_TS)
    stale = findings.get("stale", [])

    assert len(stale) >= 1, (
        "audit_bridge_mappings() must detect a stale ticket even when a ns-scale "
        "BRIDGE_ALERT exists that preceded the legacy seconds-scale SYNC. "
        f"Got stale={stale}"
    )
