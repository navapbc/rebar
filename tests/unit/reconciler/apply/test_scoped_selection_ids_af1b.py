"""Pure-function teeth for the bug-af1b fix helper ``ticket_planner.scoped_selection_ids``.

The subprocess oracle in ``test_sync_only_scalar_update_dispatch_heldout.py`` proves the
end-to-end write lands; this fast in-process test pins the helper's contract directly and
guards the opposite failure — that the expansion stays SCOPED (an unselected ticket's bound
key is never pulled into the set) and does not spuriously grow a create's local-id-only
target.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

_ENGINE_DIR = Path(__file__).resolve().parents[4] / "src" / "rebar" / "_engine"


def _load_helper() -> Any:
    mod = importlib.import_module("rebar.reconciler.ticket_planner")
    return mod.scoped_selection_ids


class _Mut:
    def __init__(self, target: str, provenance: dict) -> None:
        self.target = target
        self.provenance = provenance


def test_empty_selection_expands_to_empty() -> None:
    scoped = _load_helper()
    assert scoped(set(), [_Mut("DIG-1", {"local_id": "jira-dig-1", "jira_key": "DIG-1"})]) == []


def test_selected_bound_id_gains_its_jira_key_target() -> None:
    scoped = _load_helper()
    muts = [
        _Mut("DIG-1", {"local_id": "jira-dig-1", "jira_key": "DIG-1"}),
        _Mut("DIG-9", {"local_id": "jira-dig-9", "jira_key": "DIG-9"}),
    ]
    # Only jira-dig-1 is selected: its bound key DIG-1 is added; the UNSELECTED DIG-9 is not.
    assert scoped({"jira-dig-1"}, muts) == ["DIG-1", "jira-dig-1"]


def test_create_target_is_local_id_no_spurious_expansion() -> None:
    scoped = _load_helper()
    # A create's target IS its local id and it has no bound jira_key — the set is unchanged.
    muts = [_Mut("jira-new", {"local_id": "jira-new", "jira_key": None})]
    assert scoped({"jira-new"}, muts) == ["jira-new"]
