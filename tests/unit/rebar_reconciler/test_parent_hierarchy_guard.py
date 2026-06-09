"""Parent-hierarchy guard tests (bug 8b25): differ-side non-epic suppression and
applier-side HTTP 400 skip-and-continue.

Contract (live-proven, ticket 8b25): on this next-gen Jira project only an Epic
may be a parent. A non-epic parent (e.g. Task→Task) is rejected by Jira with
HTTP 400. Two defenses:
  - outbound_differ suppresses the parent diff when the resolved local parent's
    ticket_type != "epic" (prevents perpetual re-emission — a sync exclusion
    mirroring the 36af issuetype pattern).
  - the applier's set_parent path treats HTTP 400 as a hierarchy rejection:
    logs a WARNING and continues the pass rather than aborting.

All tests use mock clients; no live Jira calls.
"""

from __future__ import annotations

import importlib.util
import sys
import urllib.error
from io import BytesIO
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_REC = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"
OUTBOUND_DIFFER_PATH = _REC / "outbound_differ.py"
APPLIER_PATH = _REC / "applier.py"
MUTATION_PATH = _REC / "mutation.py"


def _load(name: str, path: Path) -> ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    return _load("outbound_differ_hierarchy_test", OUTBOUND_DIFFER_PATH)


@pytest.fixture(scope="module")
def applier() -> ModuleType:
    return _load("applier_hierarchy_test", APPLIER_PATH)


@pytest.fixture(scope="module")
def mutation_mod() -> ModuleType:
    return _load("mutation_hierarchy_test", MUTATION_PATH)


class StubBindingStore:
    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self._bindings = bindings or {}

    def get_jira_key(self, local_id: str) -> str | None:
        return self._bindings.get(local_id)

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._bindings


def _ticket(
    ticket_id: str,
    ticket_type: str = "task",
    parent_id: str | None = None,
) -> dict[str, Any]:
    t: dict[str, Any] = {
        "ticket_id": ticket_id,
        "title": "t",
        "description": "d",
        "status": "open",
        "priority": 2,
        "ticket_type": ticket_type,
        "assignee": "a",
        "tags": [],
        "comments": [],
    }
    if parent_id is not None:
        t["parent_id"] = parent_id
    return t


# ---------------------------------------------------------------------------
# Differ: non-epic parent suppressed (no re-emit)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_non_epic_parent_suppressed_on_create(outbound_differ: ModuleType) -> None:
    """Create with a non-epic (task) parent present in local state → parent omitted."""
    store = StubBindingStore({"parent-task": "DIG-10"})
    parent = _ticket("parent-task", ticket_type="task")
    child = _ticket("child-1", ticket_type="task", parent_id="parent-task")
    mutations = outbound_differ.compute_outbound_mutations(
        [parent, child], jira_snapshot={}, binding_store=store
    )
    create = [m for m in mutations if m.local_id == "child-1"]
    assert len(create) == 1
    assert "parent" not in create[0].fields, (
        f"non-epic parent must be suppressed, got: {create[0].fields}"
    )


@pytest.mark.unit
def test_epic_parent_still_emitted_on_create(outbound_differ: ModuleType) -> None:
    """Create with an EPIC parent present in local state → parent emitted."""
    store = StubBindingStore({"parent-epic": "DIG-20"})
    parent = _ticket("parent-epic", ticket_type="epic")
    child = _ticket("child-2", ticket_type="task", parent_id="parent-epic")
    mutations = outbound_differ.compute_outbound_mutations(
        [parent, child], jira_snapshot={}, binding_store=store
    )
    create = [m for m in mutations if m.local_id == "child-2"]
    assert len(create) == 1
    assert create[0].fields.get("parent") == "DIG-20"


@pytest.mark.unit
def test_non_epic_parent_suppressed_no_update_reemit(
    outbound_differ: ModuleType,
) -> None:
    """Bound child of a non-epic parent → no parent update re-emitted each pass."""
    store = StubBindingStore({"child-3": "DIG-30", "parent-task": "DIG-31"})
    parent = _ticket("parent-task", ticket_type="task")
    child = _ticket("child-3", ticket_type="task", parent_id="parent-task")
    # Jira side has no parent — without suppression this would re-emit forever.
    jira_snapshot = {
        "DIG-30": {
            "summary": "t",
            "description": "d",
            "issuetype": {"name": "Task"},
            "priority": {"name": "Medium"},
            "status": {"name": "To Do"},
            "assignee": {"displayName": "a"},
            "labels": [],
        }
    }
    mutations = outbound_differ.compute_outbound_mutations(
        [parent, child], jira_snapshot=jira_snapshot, binding_store=store
    )
    parent_updates = [
        m
        for m in mutations
        if m.local_id == "child-3" and "parent" in (m.fields or {})
    ]
    assert not parent_updates, (
        f"non-epic parent must not re-emit an update, got: {mutations}"
    )


# ---------------------------------------------------------------------------
# Applier: HTTP 400 on set_parent → WARNING + continue pass
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_applier_set_parent_400_continues_pass(
    applier: ModuleType,
    mutation_mod: ModuleType,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 400 on set_parent is logged and the update continues (other fields apply)."""
    import logging

    class Client:
        def __init__(self) -> None:
            self.update_calls: list[tuple] = []

        def set_parent(self, key: str, parent: str | None) -> None:
            raise urllib.error.HTTPError(
                url="x", code=400, msg="bad", hdrs=None, fp=BytesIO(b"")  # type: ignore[arg-type]
            )

        def update_issue(self, key: str, **fields: Any) -> dict:
            self.update_calls.append((key, fields))
            return {"key": key}

    client = Client()
    mut = mutation_mod.Mutation(
        direction=mutation_mod.MutationDirection.outbound,
        action=mutation_mod.MutationAction.update,
        target="DIG-50",
        payload={
            "changed_fields": {"parent": "DIG-51", "summary": "new title"},
            "comments": [],
            "labels": [],
        },
        provenance={},
    )
    with caplog.at_level(logging.WARNING):
        result = applier._apply_outbound_update(mut, client=client)

    # The non-parent field still applied — the pass continued past the 400.
    assert client.update_calls, "update_issue must still run after parent 400-skip"
    assert client.update_calls[0][1].get("summary") == "new title"
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "parent sync skipped" in joined
    assert result is not None
