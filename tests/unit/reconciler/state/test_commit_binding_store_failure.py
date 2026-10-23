"""A binding snapshot commit failure returns false, logs an error, and writes an alert; the
pass continues while warning that later merges could clobber bindings."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from rebar.reconciler import scratch_retention

REPO_ROOT = Path(__file__).resolve().parents[4]
PASS_SUPPORT_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "pass_support.py"
ALERT_STORE_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "alert_store.py"


@pytest.fixture
def pass_support_mod() -> ModuleType:
    return importlib.import_module("rebar.reconciler.pass_support")


@pytest.fixture
def alert_store_mod() -> ModuleType:
    return importlib.import_module("rebar.reconciler.alert_store")


# ---------------------------------------------------------------------------
# Test 1: commit failure → returns False + ERROR logged + alert appended
# ---------------------------------------------------------------------------


def test_commit_failure_returns_false_and_logs_error(
    tmp_path: Path, pass_support_mod: ModuleType, alert_store_mod: ModuleType, capsys
) -> None:
    """A binding snapshot commit failure returns False and writes an error to stderr so
    callers can report the clobber risk."""
    # Create bindings.json so the function doesn't early-return True
    tracker_dir = tmp_path / ".tickets-tracker"
    bridge_dir = tracker_dir / ".bridge_state"
    bridge_dir.mkdir(parents=True)
    bindings_path = bridge_dir / "bindings.json"
    bindings_path.write_text(json.dumps({"bindings": {"t1": {"jira_key": "DIG-1"}}, "reverse": {}}))

    stub_bs = MagicMock()

    import subprocess as _sp

    def _failing_run(*args, **kwargs):
        # Simulate git add succeeding but git commit failing
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and "commit" in cmd:
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            result.stderr = "error: lock file exists"
            raise _sp.CalledProcessError(1, cmd, "", "error: lock file exists")
        if isinstance(cmd, list) and "diff" in cmd and "--cached" in cmd:
            result = MagicMock()
            result.returncode = 0
            result.stdout = "bindings.json\n"
            return result
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        return result

    with patch("subprocess.run", side_effect=_failing_run):
        result = pass_support_mod._commit_binding_store_snapshot(
            stub_bs, tmp_path, "test-pass-fail-001"
        )

    assert result is False, (
        f"_commit_binding_store_snapshot must return False when git commit fails, got {result!r}"
    )

    captured = capsys.readouterr()
    assert "binding-store commit to tickets branch failed" in captured.err, (
        "An error message describing the failure must be printed to stderr. "
        f"Got stderr: {captured.err!r}"
    )


def test_commit_failure_appends_alert(
    tmp_path: Path, pass_support_mod: ModuleType, alert_store_mod: ModuleType
) -> None:
    """When git commit fails, an alert must be appended to the alert_store.

    This ensures the failure is visible to operators via bridge_alerts even
    if the reconciler log is not immediately checked.
    """
    tracker_dir = tmp_path / ".tickets-tracker"
    bridge_dir = tracker_dir / ".bridge_state"
    bridge_dir.mkdir(parents=True)
    bindings_path = bridge_dir / "bindings.json"
    bindings_path.write_text(json.dumps({"bindings": {}, "reverse": {}}))

    stub_bs = MagicMock()

    import subprocess as _sp

    def _failing_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and "commit" in cmd:
            raise _sp.CalledProcessError(1, cmd, "", "error: simulated commit failure")
        if isinstance(cmd, list) and "diff" in cmd and "--cached" in cmd:
            result = MagicMock()
            result.returncode = 0
            result.stdout = "bindings.json\n"
            return result
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        return result

    with patch("subprocess.run", side_effect=_failing_run):
        result = pass_support_mod._commit_binding_store_snapshot(
            stub_bs, tmp_path, "test-pass-alert-001"
        )

    assert result is False

    # Check that an alert was written to bridge_alerts
    alerts_dir = scratch_retention.alerts_dir(tmp_path)
    assert alerts_dir.is_dir(), (
        f"bridge_alerts directory must be created when an alert is appended. Expected: {alerts_dir}"
    )
    jsonl_files = list(alerts_dir.glob("*.jsonl"))
    assert jsonl_files, "At least one JSONL alert file must exist after a commit failure."

    all_records = []
    for jf in jsonl_files:
        for line in jf.read_text().splitlines():
            try:
                all_records.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    matching = [r for r in all_records if "binding-commit-failure" in r.get("key", "")]
    assert matching, (
        "An alert with key matching 'binding-commit-failure:*' must be appended. "
        f"Alerts found: {all_records}"
    )
    alert = matching[0]
    assert alert.get("resolved") is False, "Alert must be filed as unresolved."
    assert "timestamp_ns" in alert, "Alert must carry a timestamp_ns field."
    assert alert.get("severity") == "error", (
        f"Alert severity must be 'error', got {alert.get('severity')!r}"
    )
    assert (
        "clobber" in alert.get("reason", "").lower() or "risk" in alert.get("reason", "").lower()
    ), f"Alert reason must mention the clobber risk. Got: {alert.get('reason')!r}"


def test_commit_success_returns_true_no_alert(
    tmp_path: Path, pass_support_mod: ModuleType, alert_store_mod: ModuleType
) -> None:
    """When the locked-seam commit succeeds, _commit_binding_store_snapshot
    returns True, the commit lands in the tracker repo, and no alert is written.
    """
    import subprocess as _sp

    tracker_dir = tmp_path / ".tickets-tracker"
    bridge_dir = tracker_dir / ".bridge_state"
    bridge_dir.mkdir(parents=True)
    bindings_path = bridge_dir / "bindings.json"
    bindings_path.write_text(json.dumps({"bindings": {}, "reverse": {}}))
    # A real tracker repo: the commit now goes through the locked store seam
    # (rebar._store.push.commit_tickets_branch), which a global subprocess mock
    # cannot meaningfully fake without restating the seam's internals.
    for argv in (
        ["git", "init", "-q", str(tracker_dir)],
        ["git", "-C", str(tracker_dir), "config", "user.name", "cbsf-test"],
        ["git", "-C", str(tracker_dir), "config", "user.email", "cbsf@test.invalid"],
    ):
        _sp.run(argv, check=True, capture_output=True)

    stub_bs = MagicMock()

    result = pass_support_mod._commit_binding_store_snapshot(stub_bs, tmp_path, "test-pass-ok-001")

    assert result is True, (
        f"_commit_binding_store_snapshot must return True on success, got {result!r}"
    )

    head = _sp.run(
        ["git", "-C", str(tracker_dir), "log", "-1", "--format=%s"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "persist binding-store snapshot" in head.stdout, (
        f"the snapshot commit must land on the tracker HEAD, got {head.stdout!r}"
    )

    # No alerts should be written on success
    alerts_dir = scratch_retention.alerts_dir(tmp_path)
    if alerts_dir.is_dir():
        all_records = []
        for jf in alerts_dir.glob("*.jsonl"):
            for line in jf.read_text().splitlines():
                try:
                    all_records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        binding_alerts = [r for r in all_records if "binding-commit-failure" in r.get("key", "")]
        assert not binding_alerts, (
            f"No binding-commit-failure alert should be filed on success. Got: {binding_alerts}"
        )


def test_commit_failure_dedup_suppresses_second_alert(
    tmp_path: Path, pass_support_mod: ModuleType, alert_store_mod: ModuleType
) -> None:
    """A second commit failure for the same pass_id must not write a duplicate alert.

    Uses is_deduped gate to confirm the dedup suppression works.
    """
    tracker_dir = tmp_path / ".tickets-tracker"
    bridge_dir = tracker_dir / ".bridge_state"
    bridge_dir.mkdir(parents=True)
    bindings_path = bridge_dir / "bindings.json"
    bindings_path.write_text(json.dumps({"bindings": {}, "reverse": {}}))

    stub_bs = MagicMock()

    import subprocess as _sp

    def _failing_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and "commit" in cmd:
            raise _sp.CalledProcessError(1, cmd, "", "error: simulated")
        if isinstance(cmd, list) and "diff" in cmd and "--cached" in cmd:
            result = MagicMock()
            result.returncode = 0
            result.stdout = "bindings.json\n"
            return result
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        return result

    with patch("subprocess.run", side_effect=_failing_run):
        pass_support_mod._commit_binding_store_snapshot(stub_bs, tmp_path, "dedup-pass-001")
        # Second call with same pass_id — should be deduped
        pass_support_mod._commit_binding_store_snapshot(stub_bs, tmp_path, "dedup-pass-001")

    alerts_dir = scratch_retention.alerts_dir(tmp_path)
    if alerts_dir.is_dir():
        all_records = []
        for jf in alerts_dir.glob("*.jsonl"):
            for line in jf.read_text().splitlines():
                try:
                    all_records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        matching = [
            r for r in all_records if "binding-commit-failure:dedup-pass-001" in r.get("key", "")
        ]
        assert len(matching) == 1, (
            f"Dedup gate must suppress the second alert. Expected 1, got {len(matching)}. "
            f"Records: {matching}"
        )
