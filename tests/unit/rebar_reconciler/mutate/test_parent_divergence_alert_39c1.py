"""Parent-divergence alert contracts for outbound parent updates.

Unrepresentable parents and hierarchy rejections use distinct durable alert kinds
because their remedies differ. Transport error text remains operator evidence. Alert
delivery and alert-store failure remain non-fatal so unrelated fields can still land.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier_parent_alert_39c1", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier_parent_alert_39c1"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier():
    if not APPLIER_PATH.exists():
        pytest.fail(f"applier.py not found at {APPLIER_PATH}")
    return _load_applier()


def _alerts(root: Path) -> list[dict]:
    """Every alert record written under ``root``, in file order."""
    store = root / "bridge_state" / "bridge_alerts"
    if not store.is_dir():
        return []
    out: list[dict] = []
    for jf in sorted(store.glob("*.jsonl")):
        for line in jf.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def test_unrepresentable_parent_writes_a_bridge_alert(applier, tmp_path, monkeypatch):
    """An unrepresentable parent writes a durable alert with transport evidence."""
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    client = MagicMock()
    client.update_issue.return_value = None
    client.set_parent.side_effect = NotImplementedError(
        "DC cannot parent a non-sub-task via fields.parent"
    )
    mutation = {
        "action": "update",
        "key": "DIG-100",
        "local_id": "abcd-1234",
        "fields": {"parent": "DIG-EPIC-1"},
    }

    applier.update_one(mutation, client)

    records = _alerts(tmp_path)
    assert records, (
        "no bridge_alerts record was written for an unrepresentable parent — "
        "the divergence is still silent (39c1 AC4)"
    )
    rec = next((r for r in records if r.get("kind") == "outbound-parent-unrepresentable"), None)
    assert rec is not None, f"expected an outbound-parent-unrepresentable alert; got {records!r}"
    assert rec["key"] == "DIG-100"
    assert rec["local_id"] == "abcd-1234"
    assert rec["parent"] == "DIG-EPIC-1"
    # The reason must carry the transport's own words, or the operator learns only
    # that "something failed" and has to reproduce it to find out what.
    assert "sub-task" in rec["reason"]
    assert rec["timestamp_ns"] > 0


def test_unrepresentable_parent_is_still_non_fatal(applier, tmp_path, monkeypatch):
    """An alert does not turn a parent warning into a batch abort."""
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    client = MagicMock()
    client.update_issue.return_value = None
    client.set_parent.side_effect = NotImplementedError("nope")
    mutation = {
        "action": "update",
        "key": "DIG-101",
        "fields": {"parent": "DIG-EPIC-1", "summary": "still applied"},
    }

    applier.update_one(mutation, client)  # must not raise

    # and the rest of the mutation still went out
    assert client.update_issue.called
    _, kwargs = client.update_issue.call_args
    assert kwargs.get("summary") == "still applied"


def test_hierarchy_rejection_alerts_under_a_distinct_kind(applier, tmp_path, monkeypatch):
    """A hierarchy rejection uses the per-parent rejection alert kind."""
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    client = MagicMock()
    client.update_issue.return_value = None
    client.set_parent.side_effect = urllib.error.HTTPError(
        url="http://x", code=400, msg="bad request", hdrs=None, fp=None
    )
    mutation = {"action": "update", "key": "DIG-102", "fields": {"parent": "DIG-TASK-9"}}

    applier.update_one(mutation, client)

    kinds = [r.get("kind") for r in _alerts(tmp_path)]
    assert "outbound-parent-rejected" in kinds, (
        f"a 400 hierarchy rejection must be observable under its own kind; got {kinds!r}"
    )
    assert "outbound-parent-unrepresentable" not in kinds, (
        "a per-issue hierarchy rejection must NOT be reported as a structural gap"
    )


def test_a_successful_parent_set_writes_no_alert(applier, tmp_path, monkeypatch):
    """A successful parent update writes no divergence alert."""
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    client = MagicMock()
    client.update_issue.return_value = None
    client.set_parent.return_value = None
    mutation = {"action": "update", "key": "DIG-103", "fields": {"parent": "DIG-EPIC-1"}}

    applier.update_one(mutation, client)

    assert _alerts(tmp_path) == [], "a parent that synced cleanly must not raise an alert"


def test_a_broken_alert_store_does_not_break_the_pass(applier, tmp_path, monkeypatch):
    """Alert-store failure does not prevent other mutation fields from landing."""
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    # Occupy the alert directory's path with a FILE, so mkdir/append raise.
    (tmp_path / "bridge_state").mkdir()
    (tmp_path / "bridge_state" / "bridge_alerts").write_text("not a directory")

    client = MagicMock()
    client.update_issue.return_value = None
    client.set_parent.side_effect = NotImplementedError("nope")
    # Include a scalar field to prove alert failure does not block the remaining update.
    mutation = {
        "action": "update",
        "key": "DIG-104",
        "fields": {"parent": "DIG-EPIC-1", "summary": "still applied"},
    }

    applier.update_one(mutation, client)  # must not raise

    assert client.update_issue.called
