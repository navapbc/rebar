"""RED tests for ticket 39c1 AC4 — an unrepresentable parent must be OBSERVABLE.

Root cause: ``dispatch_one._update_one_parent`` catches every ``set_parent``
failure and emits nothing but a ``logger.warning``. A reconcile pass in which the
parent never reached the tracker therefore exits 0, reports OK, and leaves no
durable record anywhere an operator looks. That invisibility is what let the
"Cloud has the translation, DC never got its half" class run to FIVE instances
(d067, 8d68, 751e, 2b16/88d9, and this ticket) before anyone noticed.

The signal these tests require is the one the reconciler already uses for exactly
this shape of non-fatal-but-real divergence: a ``bridge_alerts`` entry, the same
channel ``apply_handlers.record_backstop_failure`` writes and that
``IllegalTransitionError`` was deliberately routed to (see
``adapters/jira_datacenter/transitions.py:34-49``). Warn-and-continue is kept —
a parent failure must still not abort the rest of the batch — but it stops being
silent.

The kinds are DISTINCT on purpose. ``outbound-parent-unrepresentable`` means the
deployment cannot express this parent at all (DC's ``set_parent`` raising
``NotImplementedError``); no retry will ever help and an operator must change the
hierarchy or the configuration. ``outbound-parent-rejected`` is Jira refusing THIS
reparent on hierarchy grounds (the 8b25 HTTP 400), which a different parent would
satisfy. Collapsing them would tell the operator a permanent structural gap and a
per-issue rejection with the same words.
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
    """DC declining a parent it cannot express must leave a durable record.

    This is AC4's core cell. ``NotImplementedError`` is DC's transport saying the
    deployment has no way to hold this relationship — after change 1302 the epic-link
    route handles the representable cases, so a decline that still reaches here is
    genuinely unrepresentable, not a missing translation.
    """
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
    """Recording the alert must not turn a warn-and-continue into an abort.

    The existing contract is explicit that a parent failure must not take down the
    rest of the batch. An alert that raises would convert five instances of silent
    divergence into a loud outage, which is not an improvement.
    """
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
    """The 8b25 HTTP 400 is a per-reparent refusal, not a structural gap.

    It gets an alert too — it was equally silent — but a different ``kind``, because
    the operator action differs: pick a valid parent, versus this deployment can
    never hold this relationship.
    """
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
    """The negative cell. Without it, an implementation that alerts unconditionally
    passes every other cell here and floods the operator's only divergence channel
    on the happy path — which would make the signal worthless exactly as fast as
    having none."""
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    client = MagicMock()
    client.update_issue.return_value = None
    client.set_parent.return_value = None
    mutation = {"action": "update", "key": "DIG-103", "fields": {"parent": "DIG-EPIC-1"}}

    applier.update_one(mutation, client)

    assert _alerts(tmp_path) == [], "a parent that synced cleanly must not raise an alert"


def test_a_broken_alert_store_does_not_break_the_pass(applier, tmp_path, monkeypatch):
    """If the alert channel itself fails, the pass must still continue.

    Observability is a secondary concern to delivery: a full disk or an unwritable
    state directory must not start failing mutations that would otherwise land.
    """
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    # Occupy the alert directory's path with a FILE, so mkdir/append raise.
    (tmp_path / "bridge_state").mkdir()
    (tmp_path / "bridge_state" / "bridge_alerts").write_text("not a directory")

    client = MagicMock()
    client.update_issue.return_value = None
    client.set_parent.side_effect = NotImplementedError("nope")
    # A summary rides along: a parent-ONLY mutation deliberately skips the scalar
    # update (there would be nothing left to send), so it cannot show that the rest
    # of the mutation survived.
    mutation = {
        "action": "update",
        "key": "DIG-104",
        "fields": {"parent": "DIG-EPIC-1", "summary": "still applied"},
    }

    applier.update_one(mutation, client)  # must not raise

    assert client.update_issue.called
