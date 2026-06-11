"""Plateau bug: description ADF round-trip strips trailing whitespace.

Jira normalizes ADF on store and drops trailing newlines/whitespace.
Local descriptions sometimes carry trailing ``\\n\\n`` (or other
trailing whitespace) that Jira will not echo back. Without
normalization, the differ emits a description update on every pass —
local pushes the trailing whitespace, Jira strips it, next fetch
shows no trailing whitespace, differ re-emits. Infinite loop.

Production blocker discovered during 20-batch bootstrap-strict live
run (2026-05-29): outbound count plateaued at 339 from batch 7
onward, with the same 10 mutations re-applied each batch and never
closing the divergence. Diagnostic on DIG-4175 showed the residual
diff was 2 trailing ``\\n\\n`` chars (local 3701, jira-decoded 3699).

Fix: rstrip both sides before comparison in BOTH outbound and inbound
differs. The comparison becomes idempotent and stable across Jira's
ADF normalization. The actual stored values are preserved (the diff
just doesn't fire on trailing-whitespace-only differences).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
OUTBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "outbound_differ.py"
)
INBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "inbound_differ.py"
)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    return _load_module("outbound_differ_rstrip", OUTBOUND_DIFFER_PATH)


@pytest.fixture(scope="module")
def inbound_differ() -> ModuleType:
    return _load_module("inbound_differ_rstrip", INBOUND_DIFFER_PATH)


class StubBindingStore:
    def __init__(self, b: dict[str, str] | None = None) -> None:
        self._b = b or {}

    def get_jira_key(self, lid):
        return self._b.get(lid)

    def is_bound(self, lid):
        return lid in self._b

    def get_local_id(self, jk):
        for lid, k in self._b.items():
            if k == jk:
                return lid
        return None


def _ticket(desc: str) -> dict:
    return {
        "ticket_id": "L1",
        "title": "T",
        "description": desc,
        "status": "open",
        "priority": 2,
        "ticket_type": "task",
        "assignee": "",
        "tags": [],
        "comments": [],
        "deps": [],
    }


def _jira(desc: str) -> dict:
    return {
        "summary": "T",
        "description": desc,
        "issuetype": "Task",
        "priority": "Medium",
        "status": "To Do",
        "assignee": "",
        "labels": [],
    }


def test_outbound_local_trailing_newlines_does_not_emit_diff(
    outbound_differ: ModuleType,
) -> None:
    """The exact phantom from DIG-4175: local has trailing \\n\\n that
    Jira strips on ADF normalization. Without rstrip-aware comparison
    the outbound differ re-emits description update every pass.
    """
    local_desc = "Story description body content.\n\n"
    jira_desc = "Story description body content."

    ticket = _ticket(local_desc)
    store = StubBindingStore({"L1": "PROJ-1"})
    snap = {"PROJ-1": _jira(jira_desc)}

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snap,
        binding_store=store,
    )

    # Either no mutation, or a mutation with no description field.
    for m in result:
        assert "description" not in m.fields, (
            f"outbound description phantom: local trailing whitespace "
            f"triggered diff: {m.fields}"
        )


def test_outbound_legitimate_description_change_still_emits(
    outbound_differ: ModuleType,
) -> None:
    """rstrip must not suppress real content changes — only trailing
    whitespace differences."""
    local_desc = "Updated description content."
    jira_desc = "Old description content."

    ticket = _ticket(local_desc)
    store = StubBindingStore({"L1": "PROJ-1"})
    snap = {"PROJ-1": _jira(jira_desc)}

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snap,
        binding_store=store,
    )

    assert len(result) == 1
    assert result[0].fields.get("description") == local_desc


def test_outbound_internal_whitespace_change_still_emits(
    outbound_differ: ModuleType,
) -> None:
    """Whitespace differences INSIDE the description body still count —
    only TRAILING whitespace is normalized."""
    local_desc = "Line one.\n\nLine two."
    jira_desc = "Line one.\nLine two."

    ticket = _ticket(local_desc)
    store = StubBindingStore({"L1": "PROJ-1"})
    snap = {"PROJ-1": _jira(jira_desc)}

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=snap,
        binding_store=store,
    )

    assert len(result) == 1
    assert "description" in result[0].fields


def test_inbound_jira_trailing_whitespace_difference_suppressed(
    inbound_differ: ModuleType,
) -> None:
    """Mirror on the inbound direction. Local has trailing whitespace,
    Jira lacks it — inbound differ should NOT emit a description
    update that would clobber local's whitespace just because Jira
    normalized it away."""
    jira_snapshot = {
        "PROJ-1": {
            "summary": "T",
            "description": "Body.",  # Jira's stripped form
            "issuetype": "Task",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "",
            "labels": [],
        }
    }
    store = StubBindingStore({"L1": "PROJ-1"})
    local_tickets = {
        "L1": {
            "title": "T",
            "description": "Body.\n\n",  # local with trailing newlines
            "ticket_type": "task",
            "priority": 2,
            "status": "open",
            "assignee": "",
            "tags": [],
        }
    }

    result, _ = inbound_differ.compute_inbound_mutations(
        jira_snapshot=jira_snapshot,
        binding_store=store,
        local_tickets_by_id=local_tickets,
    )

    for m in result:
        assert "description" not in m.fields, (
            f"inbound description phantom: trailing whitespace diff "
            f"triggered mutation: {m.fields}"
        )
