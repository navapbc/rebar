"""1368 (epic 7738): session_log tickets are NEVER synced outbound to Jira.

session_log tickets are verbose, local, agent-facing artifacts with no place in
a Jira project. ``compute_outbound_mutations`` must skip any ticket whose
``ticket_type`` is in ``config.EXCLUDED_SYNC_TYPES`` (containing ``session_log``)
— alongside the existing excluded-status check — so a dry-run reconcile over a
store containing session_logs produces ZERO mutations for them. The type is also
deliberately absent from ``_LOCAL_TO_JIRA_TYPE`` so any leak past this filter
fails loudly rather than syncing silently.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
RECONCILER_DIR = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"
OUTBOUND_DIFFER_PATH = RECONCILER_DIR / "outbound_differ.py"
CONFIG_PATH = RECONCILER_DIR / "config.py"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    return _load_module("outbound_differ_session_log", OUTBOUND_DIFFER_PATH)


@pytest.fixture(scope="module")
def reconciler_config() -> ModuleType:
    return _load_module("rebar_reconciler_config_session_log", CONFIG_PATH)


class StubBindingStore:
    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self._bindings: dict[str, str] = bindings or {}

    def get_baseline(self, local_id):
        # story d6bd: baseline arbitration is always-on; unset -> None (local-wins).
        return None

    def is_pending(self, local_id):
        return False

    def get_jira_key(self, local_id: str) -> str | None:
        return self._bindings.get(local_id)

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._bindings


def _ticket(ticket_id: str, ticket_type: str) -> dict:
    return {
        "ticket_id": ticket_id,
        "title": f"{ticket_type} ticket",
        "description": "body",
        "status": "open",
        "priority": 2,
        "ticket_type": ticket_type,
        "assignee": "alice",
        "tags": [],
        "comments": [],
        "deps": [],
    }


def test_session_log_in_excluded_sync_types(reconciler_config: ModuleType) -> None:
    assert "session_log" in reconciler_config.EXCLUDED_SYNC_TYPES


def test_session_log_absent_from_local_to_jira_type(outbound_differ: ModuleType) -> None:
    """Leak-loud invariant: session_log must NOT have a Jira issue-type mapping."""
    assert "session_log" not in outbound_differ._LOCAL_TO_JIRA_TYPE


def test_unbound_session_log_produces_no_mutation(outbound_differ: ModuleType) -> None:
    """A new (unbound) session_log must yield zero outbound mutations, while a
    sibling task in the same call still produces its CREATE — exclusion is
    surgical, not a global no-op."""
    log = _ticket("local-log-1", "session_log")
    task = _ticket("local-task-1", "task")
    store = StubBindingStore()  # nothing bound → tasks would normally CREATE

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[log, task],
        jira_snapshot={},
        binding_store=store,
    )

    log_mutations = [m for m in result if m.local_id == "local-log-1"]
    task_mutations = [m for m in result if m.local_id == "local-task-1"]
    assert log_mutations == [], f"session_log must never sync outbound; got {log_mutations}"
    assert any(m.action == "create" for m in task_mutations), (
        "the control task must still produce a CREATE — exclusion must be surgical"
    )


def test_bound_session_log_produces_no_mutation(outbound_differ: ModuleType) -> None:
    """Even a (hypothetically) bound session_log must emit no update/delete — the
    type filter precedes any binding-driven diff."""
    log = _ticket("local-log-2", "session_log")
    store = StubBindingStore({"local-log-2": "DIG-9999"})
    snapshot = {
        "DIG-9999": {
            "summary": "stale",
            "description": "stale",
            "issuetype": "Task",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "alice",
            "labels": [],
            "comment": {"comments": [], "total": 0},
        }
    }

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[log],
        jira_snapshot=snapshot,
        binding_store=store,
    )

    assert [m for m in result if m.local_id == "local-log-2"] == [], (
        "a session_log must never produce outbound mutations regardless of binding"
    )
