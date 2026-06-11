"""Tests for _commit_binding_store_snapshot failure path (Finding 3).

RED → GREEN specification:
  - commit failure (mock subprocess) → returns False + ERROR logged to stderr
    + alert appended to alert_store
  - commit success → returns True, no alert written
  - call site in reconcile_once: on False, logs loud ERROR naming the
    consequence (bindings at risk of clobber on next merge); does NOT abort pass

These tests exercise the cf93b2b7ad failure class: a silent commit failure
followed by a ``git merge origin/tickets`` loses bindings and causes the next
pass to see bound tickets as unbound.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
RECONCILE_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "reconcile.py"
ALERT_STORE_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "alert_store.py"


def _load_module(name: str, path: Path) -> ModuleType:
    key = f"_cbsf_{name}"
    if key in sys.modules:
        del sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture
def reconcile_mod() -> ModuleType:
    mod = _load_module("reconcile", RECONCILE_PATH)
    yield mod
    sys.modules.pop("_cbsf_reconcile", None)


@pytest.fixture
def alert_store_mod() -> ModuleType:
    mod = _load_module("alert_store", ALERT_STORE_PATH)
    yield mod
    sys.modules.pop("_cbsf_alert_store", None)


# ---------------------------------------------------------------------------
# Test 1: commit failure → returns False + ERROR logged + alert appended
# ---------------------------------------------------------------------------


def test_commit_failure_returns_false_and_logs_error(
    tmp_path: Path, reconcile_mod: ModuleType, alert_store_mod: ModuleType, capsys
) -> None:
    """When git commit subprocess fails, _commit_binding_store_snapshot must
    return False and print an ERROR message to stderr.

    RED: before fix, function returned None and callers could not detect failure.
    GREEN: function returns False on subprocess error.
    """
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
        result = reconcile_mod._commit_binding_store_snapshot(stub_bs, tmp_path, "test-pass-fail-001")

    assert result is False, (
        "_commit_binding_store_snapshot must return False when git commit fails, "
        f"got {result!r}"
    )

    captured = capsys.readouterr()
    assert "binding-store commit to tickets branch failed" in captured.err, (
        "An error message describing the failure must be printed to stderr. "
        f"Got stderr: {captured.err!r}"
    )


def test_commit_failure_appends_alert(
    tmp_path: Path, reconcile_mod: ModuleType, alert_store_mod: ModuleType
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

    # Pre-register the alert_store module so _load() in reconcile.py picks it up
    _alert_key = "rebar_reconciler.alert_store"
    sys.modules[_alert_key] = alert_store_mod

    try:
        with patch("subprocess.run", side_effect=_failing_run):
            result = reconcile_mod._commit_binding_store_snapshot(
                stub_bs, tmp_path, "test-pass-alert-001"
            )
    finally:
        sys.modules.pop(_alert_key, None)

    assert result is False

    # Check that an alert was written to bridge_alerts
    alerts_dir = tmp_path / "bridge_state" / "bridge_alerts"
    assert alerts_dir.is_dir(), (
        "bridge_alerts directory must be created when an alert is appended. "
        f"Expected: {alerts_dir}"
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
    assert "clobber" in alert.get("reason", "").lower() or "risk" in alert.get("reason", "").lower(), (
        "Alert reason must mention the clobber risk. "
        f"Got: {alert.get('reason')!r}"
    )


def test_commit_success_returns_true_no_alert(
    tmp_path: Path, reconcile_mod: ModuleType, alert_store_mod: ModuleType
) -> None:
    """When git commit succeeds, _commit_binding_store_snapshot returns True
    and no alert is written.
    """
    tracker_dir = tmp_path / ".tickets-tracker"
    bridge_dir = tracker_dir / ".bridge_state"
    bridge_dir.mkdir(parents=True)
    bindings_path = bridge_dir / "bindings.json"
    bindings_path.write_text(json.dumps({"bindings": {}, "reverse": {}}))

    stub_bs = MagicMock()

    def _succeeding_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        result = MagicMock()
        result.returncode = 0
        if isinstance(cmd, list) and "diff" in cmd and "--cached" in cmd:
            result.stdout = "bindings.json\n"
        else:
            result.stdout = ""
        return result

    _alert_key = "rebar_reconciler.alert_store"
    sys.modules[_alert_key] = alert_store_mod

    try:
        with patch("subprocess.run", side_effect=_succeeding_run):
            result = reconcile_mod._commit_binding_store_snapshot(
                stub_bs, tmp_path, "test-pass-ok-001"
            )
    finally:
        sys.modules.pop(_alert_key, None)

    assert result is True, (
        f"_commit_binding_store_snapshot must return True on success, got {result!r}"
    )

    # No alerts should be written on success
    alerts_dir = tmp_path / "bridge_state" / "bridge_alerts"
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
            "No binding-commit-failure alert should be filed on success. "
            f"Got: {binding_alerts}"
        )


def test_commit_failure_dedup_suppresses_second_alert(
    tmp_path: Path, reconcile_mod: ModuleType, alert_store_mod: ModuleType
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

    _alert_key = "rebar_reconciler.alert_store"
    sys.modules[_alert_key] = alert_store_mod

    try:
        with patch("subprocess.run", side_effect=_failing_run):
            reconcile_mod._commit_binding_store_snapshot(stub_bs, tmp_path, "dedup-pass-001")
            # Second call with same pass_id — should be deduped
            reconcile_mod._commit_binding_store_snapshot(stub_bs, tmp_path, "dedup-pass-001")
    finally:
        sys.modules.pop(_alert_key, None)

    alerts_dir = tmp_path / "bridge_state" / "bridge_alerts"
    if alerts_dir.is_dir():
        all_records = []
        for jf in alerts_dir.glob("*.jsonl"):
            for line in jf.read_text().splitlines():
                try:
                    all_records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        matching = [r for r in all_records if "binding-commit-failure:dedup-pass-001" in r.get("key", "")]
        assert len(matching) == 1, (
            f"Dedup gate must suppress the second alert. Expected 1, got {len(matching)}. "
            f"Records: {matching}"
        )
