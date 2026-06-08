"""Regression test for bug 68a4-f9d5-5540-4b95.

The bridge-internal binding label written by applier.py is f"dso-id:{local_id}"
(COLON separator — see applier.py around line 601). The outbound differ's
_EXCLUDED_PREFIXES previously used "dso-id-" (HYPHEN), so the differ saw the
dso-id:<UUID> label on Jira but not in local tags, and emitted a spurious
label-remove mutation. That tripped the audit guard / produced a Jira
mutation for a bridge-internal identity label that the differ should not
touch.

This test asserts that for a bound ticket whose Jira labels include
"dso-id:<local_id>" (colon form, as actually written), the outbound differ
does NOT emit any label mutation for that label.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OUTBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "outbound_differ.py"
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
    return _load_module("outbound_differ_dsoid", OUTBOUND_DIFFER_PATH)


class StubBindingStore:
    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self._bindings: dict[str, str] = bindings or {}

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


def test_outbound_differ_excludes_colon_form_dso_id_label(
    outbound_differ: ModuleType,
) -> None:
    """Jira-side ``dso-id:<UUID>`` (colon) label must NOT yield a remove mutation.

    Mirrors the real-world case: applier.py writes the binding label with a
    colon separator, so the Jira snapshot contains "dso-id:local-1", but the
    local ticket's tags do not. Without proper exclusion, the differ would
    emit a remove mutation for this bridge-internal label.
    """
    local_id = "local-1"
    ticket = _make_ticket(
        ticket_id=local_id,
        tags=["real-label"],  # no dso-id:* in local tags (as in production)
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
            "labels": [f"dso-id:{local_id}", "real-label"],
        }
    }

    result = outbound_differ.compute_outbound_mutations(
        local_tickets=[ticket],
        jira_snapshot=jira_snapshot,
        binding_store=store,
    )

    # No mutation should be emitted at all — fields match and the only
    # label difference is the bridge-internal dso-id:* label which must
    # be excluded.
    label_mutations = []
    for m in result:
        label_mutations.extend(getattr(m, "labels", []) or [])

    dso_id_mutations = [
        lm
        for lm in label_mutations
        if isinstance(lm.get("label"), str)
        and lm["label"].startswith("dso-id:")
    ]
    assert dso_id_mutations == [], (
        f"Outbound differ emitted mutations for dso-id:* labels: "
        f"{dso_id_mutations}. These are bridge-internal identity labels "
        f"and must be excluded from outbound diffs."
    )
