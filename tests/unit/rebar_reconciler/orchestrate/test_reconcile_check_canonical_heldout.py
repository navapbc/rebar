"""Ticket ad44 (HELD-OUT edge oracle): reconcile_check canonicalization edges.

Withheld from the implementer: assignee identity matching across accountId/displayName,
the RAW-jira_value report shape on a genuine divergence, the rebar-status annotation-label
CORRECTNESS improvement (B3), and the absence of a spurious send-side truncation warning.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO = Path(__file__).resolve().parents[4]
_RC = _REPO / "src" / "rebar" / "_engine" / "rebar_reconciler" / "reconcile_check.py"

pytestmark = pytest.mark.unit


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def rc() -> ModuleType:
    return _load("reconcile_check_canonical_heldout", _RC)


@pytest.fixture(scope="module")
def backend():
    from rebar_reconciler.adapters.jira.backend import JiraBackend

    return JiraBackend(transport=object())


class _StubBindingStore:
    def __init__(self, bindings):
        self._b = {lid: {"jira_key": jk, "state": "confirmed"} for lid, jk in bindings}

    def all_bindings(self):
        return dict(self._b)


def _adf(text: str) -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


def _base_jira(**ov) -> dict:
    f = {
        "summary": "T",
        "status": {"name": "To Do"},
        "priority": {"name": "Medium"},
        "issuetype": {"name": "Story"},
        "description": _adf("Hello"),
        "assignee": {"emailAddress": "user@x.com", "displayName": "User X", "accountId": "acc-1"},
        "labels": ["rebar-id:abc-1"],
    }
    f.update(ov)
    return f


def _local(**ov) -> dict:
    t = {
        "id": "abc-1",
        "title": "T",
        "status": "open",
        "priority": 2,
        "description": "Hello",
        "assignee": "user@x.com",
        "tags": [],
    }
    t.update(ov)
    return t


def _run(rc, backend, local, jira):
    store = _StubBindingStore([("abc-1", "DIG-1")])
    return rc.reconcile_check([local], {"DIG-1": jira}, store, backend=backend)


@pytest.mark.parametrize("local_assignee", ["acc-1", "User X", "user@x.com"])
def test_assignee_identity_match_across_forms(rc, backend, local_assignee) -> None:
    """Local assignee equals ANY canonical assignee_identity value (accountId / displayName /
    email) → in_sync (no assignee discrepancy)."""
    report = _run(rc, backend, _local(assignee=local_assignee), _base_jira())
    assert [d for d in report["discrepancies"] if d["field"] == "assignee"] == []


def test_divergence_reports_raw_jira_value(rc, backend) -> None:
    """A genuinely divergent status is reported with the RAW nested snapshot value as
    ``jira_value`` (report shape unchanged), while normalization suppresses the shape noise."""
    report = _run(rc, backend, _local(status="open"), _base_jira(status={"name": "Done"}))
    status_discs = [d for d in report["discrepancies"] if d["field"] == "status"]
    assert len(status_discs) == 1
    assert status_discs[0]["jira_value"] == {"name": "Done"}  # RAW snapshot value preserved
    assert report["discrepancies"] == status_discs  # status is the ONLY drift


def test_annotation_label_precedence_is_canonical(rc, backend) -> None:
    """B3 improvement: a rebar-status:blocked annotation label makes the canonical remote status
    'blocked'. A local 'blocked' ticket is therefore in_sync (the label precedence the inbound
    differ dispatches) — where the pre-ad44 RAW comparison read only the workflow status."""
    jira = _base_jira(
        status={"name": "In Progress"}, labels=["rebar-status:blocked", "rebar-id:abc-1"]
    )
    report = _run(rc, backend, _local(status="blocked"), jira)
    assert [d for d in report["discrepancies"] if d["field"] == "status"] == []


def test_no_spurious_truncation_warning(rc, backend, caplog) -> None:
    """The description comparison must NOT emit a send-side truncation warning (it uses the
    port's ADF-fit, not the warning-emitting sanitize_description) on a normal pass."""
    with caplog.at_level(logging.WARNING):
        _run(rc, backend, _local(), _base_jira())
    assert not [r for r in caplog.records if "truncat" in r.getMessage().lower()]
