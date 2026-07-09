"""Story 9622: pending-binding recovery failures are LOUD, not silently swallowed.

The old code passed the applier MODULE (no search_issues) to recover_pending_bindings
so recovery AttributeError'd into a fail-open swallow. The fix passes the real
AcliClient + a failure_sink; each failure emits a deduped alert_store bridge alert
AND surfaces a nonzero recovery_failures tally on the pass result + sync_pass_end log.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RECON_DIR = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, RECON_DIR / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def run_differs_mod():
    return _load("run_differs_loud_test", "run_differs.py")


@pytest.fixture(scope="module")
def reconcile_mod():
    return _load("reconcile_loud_test", "reconcile.py")


class _FakeLogger:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def log(self, event_type, **kwargs):
        self.events.append((event_type, kwargs))

    def close(self):
        pass


def test_emit_recovery_failure_alert_deduped(run_differs_mod):
    """A recovery failure files an 'outbound-recovery-failure' bridge alert, deduped
    per local_id over the alert_store 24h window."""
    fake_alert_store = MagicMock()
    fake_alert_store.is_deduped.return_value = False

    with patch.object(run_differs_mod, "_load", return_value=fake_alert_store):
        run_differs_mod._emit_recovery_failure_alerts(
            [{"local_id": "loc-1", "reason": "boom"}],
            repo_root=Path("/tmp/x"),
            pass_id="pass-1",
        )

    fake_alert_store.append.assert_called_once()
    (record,), kwargs = fake_alert_store.append.call_args
    assert record["kind"] == "outbound-recovery-failure"
    assert record["local_id"] == "loc-1"
    assert kwargs["repo_root"] == Path("/tmp/x")


def test_emit_recovery_failure_alert_skips_when_deduped(run_differs_mod):
    """A within-window duplicate is suppressed (no re-file)."""
    fake_alert_store = MagicMock()
    fake_alert_store.is_deduped.return_value = True  # already filed within 24h

    with patch.object(run_differs_mod, "_load", return_value=fake_alert_store):
        run_differs_mod._emit_recovery_failure_alerts(
            [{"local_id": "loc-2", "reason": "boom"}],
            repo_root=Path("/tmp/x"),
            pass_id="pass-2",
        )

    fake_alert_store.append.assert_not_called()


def test_recovery_failures_surfaced_in_result_and_log(reconcile_mod, tmp_path):
    """ctx.recovery_failures flows into the pass result dict AND the sync_pass_end log."""
    logger = _FakeLogger()
    ctx = reconcile_mod._PassContext(
        pass_id="p-123",
        repo_root=tmp_path,
        persist=False,  # skip the heavy save/snapshot block
        nowrite_plan={},  # tally short-circuits to (0, 0)
        mutations=[],
        sync_logger=logger,
        recovery_failures=2,
    )

    result = reconcile_mod._persist_and_log(ctx)

    assert result["recovery_failures"] == 2
    end_events = [kw for ev, kw in logger.events if ev == "sync_pass_end"]
    assert end_events and end_events[0]["recovery_failures"] == 2


def test_recovery_failures_defaults_zero(reconcile_mod, tmp_path):
    """A clean pass reports recovery_failures == 0."""
    logger = _FakeLogger()
    ctx = reconcile_mod._PassContext(
        pass_id="p-ok",
        repo_root=tmp_path,
        persist=False,
        nowrite_plan={},
        mutations=[],
        sync_logger=logger,
    )
    result = reconcile_mod._persist_and_log(ctx)
    assert result["recovery_failures"] == 0
