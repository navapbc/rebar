"""Ticket ad44: reconcile_check canonicalization (happy path).

reconcile_check stops comparing in raw Jira shape. It canonicalizes each bound snapshot
entry via the injected ``InboundMapper.map_remote_to_local`` (reusing 625b's canonical
keys) and compares in LOCAL vocabulary; the local description is ADF-fit through the
injected ``OutboundMapper.map_fields_to_remote`` (no send-side warning), mirroring the
canonical differ. The reported ``jira_value`` stays the RAW snapshot value.

Injection seam: ``reconcile_check(..., backend=<Backend>)`` — tests pass a
``JiraBackend(transport=object())`` whose mappers are pure, so no env / live client is
needed (mirrors outbound_differ's select_backend fallback for production).
"""

from __future__ import annotations

import importlib.util
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
    return _load("reconcile_check_canonical", _RC)


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


def test_live_shaped_in_sync_pair_zero_discrepancies(rc, backend) -> None:
    """A healthy live-shape binding (nested status/priority, ADF description, dict assignee)
    whose canonical values agree with local reports ZERO discrepancies — the runny-lens-strafe
    regression, now via canonical mapping (no vendor by-path helpers)."""
    local = [
        {
            "id": "abc-1",
            "title": "T",
            "status": "open",
            "priority": 2,
            "description": "Hello",
            "assignee": "user@x.com",
            "tags": ["team:backend"],
        }
    ]
    snapshot = {
        "DIG-1": {
            "summary": "T",
            "status": {"name": "To Do"},
            "priority": {"name": "Medium"},
            "issuetype": {"name": "Story"},
            "description": _adf("Hello"),
            "assignee": {
                "emailAddress": "user@x.com",
                "displayName": "User X",
                "accountId": "acc-1",
            },
            "labels": ["team:backend", "rebar-id:abc-1"],
        }
    }
    store = _StubBindingStore([("abc-1", "DIG-1")])
    report = rc.reconcile_check(local, snapshot, store, backend=backend)
    assert report["checked"] == 1
    assert report["in_sync"] == 1
    assert report["discrepancies"] == []
