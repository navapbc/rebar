"""Tests for _commit_binding_store_snapshot failure path (Finding 3).

RED → GREEN specification:
  - commit-phase failure (seam raises a commit-phase ``PushDeliveryError``) →
    returns False + ERROR logged to stderr + alert appended to alert_store
  - commit success (seam returns) → returns True, no alert written
  - call site in reconcile_once: on False, logs loud ERROR naming the
    consequence (bindings at risk of clobber on next merge); does NOT abort pass

These tests exercise the clobbered-bindings failure class: a silent commit failure
followed by a ``git merge origin/tickets`` loses bindings and causes the next
pass to see bound tickets as unbound.

Ticket ``6454-d06e-7361-4e3d`` re-pointed these tests off the retired raw
``subprocess.run`` staging/commit mechanism and onto the authoritative
``rebar._store.push.commit_and_push_tickets_branch`` seam the helper now delegates
to. A COMMIT-phase ``PushDeliveryError`` (the locked commit itself never landed)
is the fail-open + alert case; a PUSH-phase failure (the commit landed, delivery
is best-effort) is NOT — that is covered by the delegation test in
``tests/unit/rebar_reconciler/test_reconcile_binding_snapshot.py``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from rebar._store.push_classify import PushDeliveryError

REPO_ROOT = Path(__file__).resolve().parents[4]
RECONCILE_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "reconcile.py"
ALERT_STORE_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "alert_store.py"

_SEAM = "rebar._store.push.commit_and_push_tickets_branch"


def _commit_phase_error() -> PushDeliveryError:
    """A ``PushDeliveryError`` whose reason names the LOCKED-COMMIT phase.

    The helper re-raises (fail-open + alert) only for commit-phase reasons; a
    push-phase reason means the commit already landed and is swallowed as success.
    """
    return PushDeliveryError("commit-failed", "simulated commit failure", "/x", "origin/tickets")


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
# Test 1: commit-phase failure → returns False + ERROR logged + alert appended
# ---------------------------------------------------------------------------


def test_commit_failure_returns_false_and_logs_error(
    tmp_path: Path, reconcile_mod: ModuleType, alert_store_mod: ModuleType, capsys
) -> None:
    """When the seam raises a COMMIT-phase failure, _commit_binding_store_snapshot
    must return False and print an ERROR message to stderr.

    RED: before fix, function returned None and callers could not detect failure.
    GREEN: function returns False on a commit-phase seam failure.
    """
    tracker_dir = tmp_path / ".tickets-tracker"
    bridge_dir = tracker_dir / ".bridge_state"
    bridge_dir.mkdir(parents=True)
    bindings_path = bridge_dir / "bindings.json"
    bindings_path.write_text(json.dumps({"bindings": {"t1": {"jira_key": "DIG-1"}}, "reverse": {}}))

    stub_bs = MagicMock()

    with patch(_SEAM, side_effect=_commit_phase_error()):
        result = reconcile_mod._commit_binding_store_snapshot(
            stub_bs, tmp_path, "test-pass-fail-001"
        )

    assert result is False, (
        f"_commit_binding_store_snapshot must return False when the locked commit fails, "
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
    """When the locked commit fails, an alert must be appended to the alert_store.

    This ensures the failure is visible to operators via bridge_alerts even
    if the reconciler log is not immediately checked.
    """
    tracker_dir = tmp_path / ".tickets-tracker"
    bridge_dir = tracker_dir / ".bridge_state"
    bridge_dir.mkdir(parents=True)
    bindings_path = bridge_dir / "bindings.json"
    bindings_path.write_text(json.dumps({"bindings": {}, "reverse": {}}))

    stub_bs = MagicMock()

    # Pre-register the alert_store module so _load() in reconcile.py picks it up
    _alert_key = "rebar_reconciler.alert_store"
    sys.modules[_alert_key] = alert_store_mod

    try:
        with patch(_SEAM, side_effect=_commit_phase_error()):
            result = reconcile_mod._commit_binding_store_snapshot(
                stub_bs, tmp_path, "test-pass-alert-001"
            )
    finally:
        sys.modules.pop(_alert_key, None)

    assert result is False

    # Check that an alert was written to bridge_alerts
    alerts_dir = tmp_path / "bridge_state" / "bridge_alerts"
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
    tmp_path: Path, reconcile_mod: ModuleType, alert_store_mod: ModuleType
) -> None:
    """When the seam commits successfully, _commit_binding_store_snapshot returns
    True and no alert is written.
    """
    tracker_dir = tmp_path / ".tickets-tracker"
    bridge_dir = tracker_dir / ".bridge_state"
    bridge_dir.mkdir(parents=True)
    bindings_path = bridge_dir / "bindings.json"
    bindings_path.write_text(json.dumps({"bindings": {}, "reverse": {}}))

    stub_bs = MagicMock()

    _alert_key = "rebar_reconciler.alert_store"
    sys.modules[_alert_key] = alert_store_mod

    try:
        with patch(_SEAM, return_value=None):
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
            f"No binding-commit-failure alert should be filed on success. Got: {binding_alerts}"
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

    _alert_key = "rebar_reconciler.alert_store"
    sys.modules[_alert_key] = alert_store_mod

    try:
        with patch(_SEAM, side_effect=_commit_phase_error()):
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
        matching = [
            r for r in all_records if "binding-commit-failure:dedup-pass-001" in r.get("key", "")
        ]
        assert len(matching) == 1, (
            f"Dedup gate must suppress the second alert. Expected 1, got {len(matching)}. "
            f"Records: {matching}"
        )
