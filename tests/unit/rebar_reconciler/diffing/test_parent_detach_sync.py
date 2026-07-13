"""Parent DETACH must sync to Jira so the reconciler stops re-parenting (churn bug).

Root cause: the outbound parent sync is asymmetric — it propagates a parent SET to Jira's
epic-link but NEVER a parent CLEAR. So a local detach (`edit --parent=null`) leaves a STALE
Jira epic-link, and the inbound differ unconditionally re-applies it every reconcile pass,
silently reverting the detach within minutes. The fix makes the outbound parent sync
SYMMETRIC (emit a clear), which also drives the existing same-pass inbound suppression so the
detach is not reverted.

Managed-ref gating (tan-elbow-mica / story safe-luge-nog): the CLEAR is propagated ONLY for
a parent we MANAGED (in the ticket's ``managed_refs``). A parent a human set directly in Jira
on a locally-parentless ticket — one rebar never managed — is left for inbound ADOPT, NOT
clobbered. So the detached child here carries the parent in ``managed_refs``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
OUTBOUND_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "outbound_differ.py"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    return _load("outbound_differ", OUTBOUND_PATH)


class StubBindingStore:
    def __init__(self, bindings: dict[str, str]) -> None:
        self._bindings = bindings  # {local_id: jira_key}

    def get_baseline(self, local_id):
        # story d6bd: baseline arbitration is always-on; unset -> None (local-wins).
        return None

    def is_pending(self, local_id):
        return False

    def get_jira_key(self, local_id: str) -> str | None:
        return self._bindings.get(local_id)

    def get_local_id(self, jira_key: str) -> str | None:
        for local_id, key in self._bindings.items():
            if key == jira_key:
                return local_id
        return None

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._bindings


def test_detach_emits_outbound_parent_clear(outbound_differ: ModuleType) -> None:
    """A locally-DETACHED ticket whose Jira snapshot still shows a parent we MANAGED must emit
    an outbound parent CLEAR. Otherwise the stale Jira epic-link is re-applied inbound each pass."""
    # Local child, deliberately DETACHED (parent_id cleared), all other fields matching Jira.
    # managed_refs records the parent we used to hold (local-epic) — so the detach propagates.
    child = {
        "ticket_id": "local-child",
        "title": "Fix the widget",
        "description": "It is broken",
        "status": "open",
        "priority": 2,
        "ticket_type": "task",
        "assignee": "alice",
        "tags": [],
        "comments": [],
        "deps": [],
        "parent_id": None,  # ← detached locally
        "managed_refs": [["parent", "local-epic"]],  # ← we MANAGED this parent, then detached
    }
    store = StubBindingStore({"local-child": "PROJ-1", "local-epic": "PROJ-EPIC"})
    jira_snapshot = {
        "PROJ-1": {
            "summary": "Fix the widget",
            "description": "It is broken",
            "issuetype": "Task",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "alice",
            "labels": [],
            "parent": {"key": "PROJ-EPIC"},  # ← STALE epic-link still in Jira
        }
    }

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[child],
        jira_snapshot=jira_snapshot,
        binding_store=store,
    )

    parent_muts = [m for m in result if m.jira_key == "PROJ-1" and "parent" in (m.fields or {})]
    assert parent_muts, (
        "outbound differ must emit a parent change for PROJ-1 when the ticket is detached "
        "locally but Jira still links it to PROJ-EPIC (the stale epic-link must be cleared)"
    )
    assert not parent_muts[0].fields["parent"], (
        "the emitted parent value must be a CLEAR (None/empty) — clearing the stale Jira "
        f"epic-link — not a re-set; got {parent_muts[0].fields['parent']!r}"
    )


def test_never_parented_ticket_emits_no_parent_clear(outbound_differ: ModuleType) -> None:
    """The symmetric-clear must NOT churn the other way: a bound ticket that was never parented
    (local parent_id None, Jira also has no parent) must emit NO parent mutation every pass."""
    child = {
        "ticket_id": "local-child",
        "title": "Fix the widget",
        "description": "It is broken",
        "status": "open",
        "priority": 2,
        "ticket_type": "task",
        "assignee": "alice",
        "tags": [],
        "comments": [],
        "deps": [],
        "parent_id": None,  # never had a parent
    }
    store = StubBindingStore({"local-child": "PROJ-1"})
    jira_snapshot = {
        "PROJ-1": {
            "summary": "Fix the widget",
            "description": "It is broken",
            "issuetype": "Task",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "alice",
            "labels": [],
            # no "parent" key — Jira side is also parent-less
        }
    }

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[child],
        jira_snapshot=jira_snapshot,
        binding_store=store,
    )

    parent_muts = [m for m in result if "parent" in (m.fields or {})]
    assert not parent_muts, (
        "a never-parented bound ticket (local None, Jira None) must emit NO parent mutation — "
        f"the unconditional 'parent: None' sentinel must not churn a clear every pass; got {result}"
    )


def test_unmanaged_jira_parent_is_not_cleared(outbound_differ: ModuleType) -> None:
    """Managed-ref gate (tan-elbow-mica): a parent a human set DIRECTLY in Jira on a
    locally-parentless ticket — one rebar never managed (absent from managed_refs) — must NOT be
    cleared. It is left for inbound ADOPT; clearing it would clobber the human's edit."""
    child = {
        "ticket_id": "local-child",
        "title": "Fix the widget",
        "description": "It is broken",
        "status": "open",
        "priority": 2,
        "ticket_type": "task",
        "assignee": "alice",
        "tags": [],
        "comments": [],
        "deps": [],
        "parent_id": None,  # locally parentless
        "managed_refs": [],  # ← we NEVER managed any parent for this ticket
    }
    store = StubBindingStore({"local-child": "PROJ-1", "local-epic": "PROJ-EPIC"})
    jira_snapshot = {
        "PROJ-1": {
            "summary": "Fix the widget",
            "description": "It is broken",
            "issuetype": "Task",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "alice",
            "labels": [],
            "parent": {"key": "PROJ-EPIC"},  # ← a human set this parent in Jira
        }
    }

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[child],
        jira_snapshot=jira_snapshot,
        binding_store=store,
    )

    parent_muts = [m for m in result if "parent" in (m.fields or {})]
    assert not parent_muts, (
        "an unmanaged (human-set-in-Jira) parent must NOT be cleared — it is adopted inbound, "
        f"not clobbered; got {result}"
    )
