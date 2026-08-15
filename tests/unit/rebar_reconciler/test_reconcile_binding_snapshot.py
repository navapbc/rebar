"""Behavior pins for the seam conversion of ``_commit_binding_store_snapshot``.

Ticket 6454-d06e-7361-4e3d: the binding-store snapshot commit must route through the
shared, write-lock-protected ``rebar._store.push.commit_and_push_tickets_branch`` seam
(strict=True) rather than the lock-bypassing raw ``git_adapter.add`` + ``commit``. The
behavior contract the conversion must preserve, pinned here:

* The helper DELEGATES to ``commit_and_push_tickets_branch`` with the tracker dir and
  ``strict=True`` (so a failure of the locked commit phase is observable).
* Fail-open is preserved: a failure of the LOCKED COMMIT phase (the bindings did not get
  committed → at risk of a ``git merge origin/tickets`` clobber) returns ``False`` and
  appends a ``binding-commit-failure`` alert; the pass is NOT aborted.
* A PUSH-phase failure is NOT the at-risk case: the locked commit already succeeded, so the
  bindings are durably on the local tickets branch (delivery is best-effort / PUSH_PENDING).
  The helper returns ``True`` and files no alert — matching the prior behavior, which never
  pushed and returned ``True`` after a successful commit.
* Success returns ``True`` and files no alert.
* The intended ``git add -A`` WIDENING (vs the prior selective 5-file staging) is exercised
  end-to-end against a real tickets git repo: an unrelated pending ``.bridge_state`` file is
  committed in the same locked commit.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from rebar._store import push
from rebar._store.push_classify import PushDeliveryError

REPO_ROOT = Path(__file__).resolve().parents[3]
RECONCILE_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "reconcile.py"
ALERT_STORE_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "alert_store.py"


def _load_module(name: str, path: Path) -> ModuleType:
    key = f"_rbs_{name}"
    if key in sys.modules:
        del sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def reconcile_mod() -> ModuleType:
    mod = _load_module("reconcile", RECONCILE_PATH)
    yield mod
    sys.modules.pop("_rbs_reconcile", None)


@pytest.fixture
def alert_store_mod() -> ModuleType:
    mod = _load_module("alert_store", ALERT_STORE_PATH)
    # Register under the package name the helper's _load() resolves, so the alert append
    # in the fail-open branch reuses this module rather than loading a fresh copy.
    sys.modules["rebar_reconciler.alert_store"] = mod
    yield mod
    sys.modules.pop("rebar_reconciler.alert_store", None)
    sys.modules.pop("_rbs_alert_store", None)


def _make_tracker(tmp_path: Path) -> Path:
    tracker = tmp_path / ".tickets-tracker"
    bridge = tracker / ".bridge_state"
    bridge.mkdir(parents=True)
    (bridge / "bindings.json").write_text(json.dumps({"bindings": {}, "reverse": {}}))
    return tracker


def _binding_alerts(repo_root: Path) -> list[dict]:
    alerts_dir = repo_root / "bridge_state" / "bridge_alerts"
    records: list[dict] = []
    if alerts_dir.is_dir():
        for jf in alerts_dir.glob("*.jsonl"):
            for line in jf.read_text().splitlines():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return [r for r in records if "binding-commit-failure" in r.get("key", "")]


def test_delegates_to_seam_with_strict(tmp_path, reconcile_mod, monkeypatch) -> None:
    """The helper calls commit_and_push_tickets_branch(tracker_dir, strict=True)."""
    tracker = _make_tracker(tmp_path)
    calls: list[dict] = []

    def _fake(tracker_arg, *, message, strict=False, **kw):
        calls.append({"tracker": str(tracker_arg), "message": message, "strict": strict})

    monkeypatch.setattr(push, "commit_and_push_tickets_branch", _fake)
    ok = reconcile_mod._commit_binding_store_snapshot(MagicMock(), tmp_path, "pass-1")
    assert ok is True
    assert len(calls) == 1, "helper must call the shared locked commit+push seam exactly once"
    assert calls[0]["strict"] is True, "must call the seam with strict=True to observe failures"
    assert str(tracker) in calls[0]["tracker"], "must target the .tickets-tracker dir"
    assert "pass-1" in calls[0]["message"]


def test_commit_phase_failure_is_failopen_false_and_alert(
    tmp_path, reconcile_mod, alert_store_mod, capsys, monkeypatch
) -> None:
    """A LOCKED-COMMIT-phase PushDeliveryError -> returns False + files an alert."""
    tracker = _make_tracker(tmp_path)

    def _fake(tracker_arg, *, message, strict=False, **kw):
        raise PushDeliveryError("commit-failed", "simulated", str(tracker), "origin/tickets")

    monkeypatch.setattr(push, "commit_and_push_tickets_branch", _fake)
    ok = reconcile_mod._commit_binding_store_snapshot(MagicMock(), tmp_path, "pass-fail")
    assert ok is False, "a locked-commit failure must return False (bindings at risk)"
    assert "binding-store commit to tickets branch failed" in capsys.readouterr().err
    alerts = _binding_alerts(tmp_path)
    assert alerts, "a binding-commit-failure alert must be appended on a commit-phase failure"
    assert alerts[0].get("severity") == "error"
    assert alerts[0].get("resolved") is False


def test_push_phase_failure_returns_true_no_alert(
    tmp_path, reconcile_mod, alert_store_mod, monkeypatch
) -> None:
    """A PUSH-phase failure is best-effort: commit already landed -> True, no alert."""
    tracker = _make_tracker(tmp_path)

    def _fake(tracker_arg, *, message, strict=False, **kw):
        raise PushDeliveryError("remote-not-found", "no remote", str(tracker), "origin/tickets")

    monkeypatch.setattr(push, "commit_and_push_tickets_branch", _fake)
    ok = reconcile_mod._commit_binding_store_snapshot(MagicMock(), tmp_path, "pass-push")
    assert ok is True, "a push-phase failure must not be treated as a binding-commit failure"
    assert not _binding_alerts(tmp_path), "no alert on a mere delivery (push) failure"


def test_success_returns_true_no_alert(
    tmp_path, reconcile_mod, alert_store_mod, monkeypatch
) -> None:
    _make_tracker(tmp_path)
    monkeypatch.setattr(push, "commit_and_push_tickets_branch", lambda *a, **k: None)
    ok = reconcile_mod._commit_binding_store_snapshot(MagicMock(), tmp_path, "pass-ok")
    assert ok is True
    assert not _binding_alerts(tmp_path)


def _init_tickets_repo(tracker: Path) -> None:
    subprocess.run(["git", "init", "-b", "tickets", str(tracker)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t.com"), ("user.name", "Test")):
        subprocess.run(["git", "-C", str(tracker), "config", k, v], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tracker), "commit", "--allow-empty", "-m", "init", "--no-verify"],
        check=True,
        capture_output=True,
    )


def _file_in_head(tracker: Path, rel: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(tracker), "show", f"HEAD:{rel}"], capture_output=True
        ).returncode
        == 0
    )


def test_git_add_a_widening_commits_all_pending_bridge_state(
    tmp_path, reconcile_mod, alert_store_mod, monkeypatch
) -> None:
    """End-to-end through the REAL seam: git add -A commits every pending .bridge_state
    file (the intended widening), including one that is NOT a binding-store file. The push
    step is neutralized so no remote is required."""
    tracker = tmp_path / ".tickets-tracker"
    bridge = tracker / ".bridge_state"
    _init_tickets_repo(tracker)
    bridge.mkdir(parents=True)
    (bridge / "bindings.json").write_text(json.dumps({"bindings": {"a": 1}, "reverse": {}}))
    # An unrelated tickets-branch state file, also pending:
    (bridge / "projects.json").write_text(json.dumps({"projects": {}, "legacy_default": None}))

    # Neutralize the push so the real locked commit runs without a remote.
    monkeypatch.setattr(push, "push_tickets_branch", lambda *a, **k: None)

    ok = reconcile_mod._commit_binding_store_snapshot(MagicMock(), tmp_path, "pass-widen")
    assert ok is True
    assert _file_in_head(tracker, ".bridge_state/bindings.json")
    assert _file_in_head(tracker, ".bridge_state/projects.json"), (
        "the seam's git add -A must commit all pending .bridge_state state under the lock"
    )
