"""DESIRED-behavior tests for Jira link / relationship sync (TDD spec).

These tests encode the CONTRACT we want — that rebar↔Jira ticket links
(blocks / depends_on / relates_to / ...) actually sync in both directions —
NOT necessarily the contract the code implements today. They are written as
plain asserting tests (not pre-marked xfail) so the run reveals true red/green:
a red result is an informative finding that becomes the TDD spec for wiring
link sync into the differs.

Loaded via ``spec_from_file_location`` per the reconciler test-tree convention
(mirrors test_reconcile_roundtrip.py).

Empirical context (current state — link sync IS implemented; story 25ae):

* Local ticket dicts (from ``rebar list`` JSON) carry links under the ``deps``
  key, shaped as ``[{"target_id", "relation", "link_uuid"}, ...]`` where
  ``relation`` ∈ {blocks, depends_on, relates_to, duplicates, supersedes,
  discovered_from}. The local side carries link data into the differ.
* ``outbound_differ.OutboundMutation`` has a ``links: list`` field that is now
  POPULATED by ``_diff_links`` in the update branch of
  ``compute_outbound_mutations``: it reads ``deps``, maps each relation to a
  Jira link type via ``_RELATION_TO_JIRA_LINK``, and dedups against the issue's
  existing ``issuelinks`` (ADD-only; create-branch links are resolved after all
  creates). The applier dispatches each add via ``client.set_relationship`` and
  (bug d843) does a live pre-create ``get_issue_links`` existence check.
* ``inbound_differ.InboundMutation`` now HAS a ``links`` field, populated by
  ``_diff_links_inbound``, which reverse-maps an issue's ``issuelinks`` array
  back to rebar relations (ADD-only, deduped against existing local deps).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
RECONCILER_DIR = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def outbound() -> ModuleType:
    return _load_module("outbound_differ", RECONCILER_DIR / "outbound_differ.py")


@pytest.fixture(scope="module")
def inbound() -> ModuleType:
    return _load_module("inbound_differ", RECONCILER_DIR / "inbound_differ.py")


class StubBindingStore:
    """Serves both directions over one local_id<->jira_key map (per roundtrip)."""

    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self._l2j: dict[str, str] = bindings or {}
        self._j2l: dict[str, str] = {v: k for k, v in self._l2j.items()}

    def get_jira_key(self, local_id: str) -> str | None:
        return self._l2j.get(local_id)

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._l2j

    def get_local_id(self, jira_key: str) -> str | None:
        return self._j2l.get(jira_key)

    # baseline arbitration surface (always-on since story d6bd)
    def is_pending(self, local_id: str) -> bool:
        return False

    def get_baseline(self, local_id: str) -> dict | None:
        return None


def _make_ticket(
    ticket_id: str,
    *,
    title: str = "Some ticket",
    status: str = "open",
    deps: list[dict] | None = None,
) -> dict:
    """Build a local ticket dict in the shape ``rebar list`` emits.

    ``deps`` carries the link data in the real local shape:
    ``[{"target_id", "relation", "link_uuid"}, ...]``.
    """
    return {
        "ticket_id": ticket_id,
        "title": title,
        "description": "A description long enough to be realistic for the differ.",
        "status": status,
        "priority": 2,
        "ticket_type": "task",
        "assignee": "alice",
        "tags": [],
        "comments": [],
        "deps": deps or [],
        "parent_id": None,
    }


# ===========================================================================
# A1. Outbound emits a link mutation for a local blocks/depends_on link.
# ===========================================================================


def test_outbound_emits_link_mutation_for_local_blocks_link(outbound):
    """DESIRED: a local ``blocks`` link must produce an outbound link op.

    Two bound local tickets A and B; A has ``deps=[{target_id:B,
    relation:'blocks'}]``. The desired contract is that
    ``compute_outbound_mutations`` emits, on A's mutation, a ``links`` entry
    targeting B's Jira key (so the applier would call
    ``set_relationship(A_key, B_key, 'Blocks')``).

    NOTE: this encodes desired (possibly not-yet-implemented) behavior.
    """
    bind = StubBindingStore({"loc-a": "DIG-100", "loc-b": "DIG-200"})
    a = _make_ticket(
        "loc-a",
        title="Blocker",
        deps=[{"target_id": "loc-b", "relation": "blocks", "link_uuid": "u1"}],
    )
    b = _make_ticket("loc-b", title="Blocked")

    muts, _ = outbound.compute_outbound_mutations([a, b], {"DIG-100": {}, "DIG-200": {}}, bind)

    a_mut = next((m for m in muts if m.local_id == "loc-a"), None)
    all_links = [lk for m in muts for lk in (m.links or [])]
    assert a_mut is not None and a_mut.links, (
        "outbound emitted NO link mutation for a local 'blocks' link. "
        f"All outbound mutations: "
        f"{[(m.local_id, m.action, dict(fields=m.fields, links=m.links)) for m in muts]}. "
        f"Aggregated links across all mutations: {all_links}"
    )
    # The link op references B's bound Jira key under the settled ``to_key`` key
    # (the apply contract is fixed — _diff_links emits ``to_key`` and the applier
    # consumes ``to_key``; the prior to_key/target/to triple-fallback is gone).
    targets = {lk.get("to_key") for lk in a_mut.links}
    assert "DIG-200" in targets, (
        f"outbound link mutation does not target B's Jira key DIG-200: {a_mut.links}"
    )


# ===========================================================================
# A2. Inbound reflects a Jira issuelink into rebar.
# ===========================================================================


def test_inbound_reflects_jira_issuelink_into_rebar(inbound):
    """DESIRED: a Jira ``issuelinks`` entry must produce an inbound link change.

    Build a Jira-shape snapshot for a bound issue whose ``issuelinks`` array
    carries a Blocks link to another bound issue. The desired contract is that
    ``compute_inbound_mutations`` emits, on the corresponding local ticket, a
    relation/link change reflecting that Jira link into rebar's ``deps``.

    The ``issuelinks`` shape used here is the Jira REST v3 form
    (``get_issue_links`` documents ``[{"type": {"name": ...},
    "inwardIssue"|"outwardIssue": {...}}]``). The live probe
    (tests/external/test_link_sync_live.py) captures the EXACT live shape; this
    fixture mirrors that documented shape.

    NOTE: this encodes desired (possibly not-yet-implemented) behavior.
    """
    bind = StubBindingStore({"loc-a": "DIG-100", "loc-b": "DIG-200"})
    a = _make_ticket("loc-a", title="Blocker", deps=[])
    b = _make_ticket("loc-b", title="Blocked", deps=[])

    # Jira-shape snapshot: DIG-100 "blocks" DIG-200 via an outward Blocks link.
    jira_snapshot = {
        "DIG-100": {
            "summary": "Blocker",
            "status": {"name": "To Do"},
            "issuetype": {"name": "Task"},
            "priority": {"name": "Medium"},
            "assignee": {"displayName": "alice"},
            "labels": [],
            "issuelinks": [
                {
                    "id": "55001",
                    "type": {
                        "name": "Blocks",
                        "inward": "is blocked by",
                        "outward": "blocks",
                    },
                    "outwardIssue": {"key": "DIG-200"},
                }
            ],
        },
        "DIG-200": {
            "summary": "Blocked",
            "status": {"name": "To Do"},
            "issuetype": {"name": "Task"},
            "priority": {"name": "Medium"},
            "assignee": {"displayName": "alice"},
            "labels": [],
            "issuelinks": [],
        },
    }

    inbound_muts, _ = inbound.compute_inbound_mutations(
        jira_snapshot, bind, {"loc-a": a, "loc-b": b}
    )

    # The desired signal: an inbound mutation for loc-a that carries a link/dep
    # change reflecting the Jira "blocks DIG-200" link back into rebar.
    a_mut = next((m for m in inbound_muts if m.local_id == "loc-a"), None)
    a_links = list(getattr(a_mut, "links", []) or []) if a_mut is not None else []
    a_dep_field = (a_mut.fields.get("deps") if a_mut is not None else None) or (
        a_mut.fields.get("links") if a_mut is not None else None
    )
    dump = [
        (m.local_id, m.action, m.fields, getattr(m, "links", "<no links attr>"))
        for m in inbound_muts
    ]
    assert a_links or a_dep_field, (
        f"inbound reflected NO link/relation change for a Jira issuelink. Inbound mutations: {dump}"
    )
