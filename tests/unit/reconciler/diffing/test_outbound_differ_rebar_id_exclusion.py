"""The outbound differ must preserve Jira's internal rebar-id:<UUID> binding label."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
OUTBOUND_DIFFER_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "outbound_differ.py"


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    return importlib.import_module("rebar.reconciler.outbound_differ")


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


def _make_ticket(
    ticket_id: str,
    tags: list[str] | None = None,
) -> dict:
    return {
        "ticket_id": ticket_id,
        "title": "Fix the widget",
        "description": "It is broken",
        "status": "open",
        "priority": 2,
        "ticket_type": "bug",
        "assignee": "alice",
        "tags": tags or [],
        "comments": [],
        "deps": [],
    }


def test_outbound_differ_excludes_colon_form_rebar_id_label(
    outbound_differ: ModuleType,
) -> None:
    """A Jira-only rebar-id:<UUID> label must not produce a label-removal mutation."""
    local_id = "local-1"
    ticket = _make_ticket(
        ticket_id=local_id,
        tags=["real-label"],  # no rebar-id:* in local tags (as in production)
    )
    store = StubBindingStore({local_id: "PROJ-100"})
    jira_snapshot = {
        "PROJ-100": {
            "summary": "Fix the widget",
            "description": "It is broken",
            "issuetype": "Bug",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "alice",
            "labels": [f"rebar-id:{local_id}", "real-label"],
        }
    }

    result, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=jira_snapshot,
        binding_store=store,
    )

    # No mutation should be emitted at all — fields match and the only
    # label difference is the bridge-internal rebar-id:* label which must
    # be excluded.
    label_mutations = []
    for m in result:
        label_mutations.extend(getattr(m, "labels", []) or [])

    rebar_id_mutations = [
        lm
        for lm in label_mutations
        if isinstance(lm.get("label"), str) and lm["label"].startswith("rebar-id:")
    ]
    assert rebar_id_mutations == [], (
        f"Outbound differ emitted mutations for rebar-id:* labels: "
        f"{rebar_id_mutations}. These are bridge-internal identity labels "
        f"and must be excluded from outbound diffs."
    )
