"""Deterministic CI coverage for link-dedup / echo-suppression (bug d843 C1).

The link dedup paths (outbound ``_diff_links`` and inbound
``_diff_links_inbound``) previously had ZERO deterministic CI coverage — they
were exercised only in gated external (live-Jira) tests. These two cases pin the
"already present → no re-emit" contract that the write-safe retry relies on:

  (a) outbound: a local ``blocks`` dep whose Jira ``issuelinks`` ALREADY carries
      the matching (Blocks, target_key) entry → ``_diff_links`` returns [].
  (b) inbound: a Jira issuelink ALREADY present in the local ticket's deps →
      ``_diff_links_inbound`` returns [].

Uses the importlib spec_from_file_location pattern established in the reconciler
test tree (see conftest.py docstring for rationale).
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
    return _load_module("outbound_differ", OUTBOUND_DIFFER_PATH)


@pytest.fixture(scope="module")
def inbound_differ() -> ModuleType:
    return _load_module("inbound_differ", INBOUND_DIFFER_PATH)


class StubBindingStore:
    """In-memory binding store: maps local_id <-> jira_key both directions."""

    def __init__(self, bindings: dict[str, str] | None = None) -> None:
        self._fwd: dict[str, str] = bindings or {}
        self._rev: dict[str, str] = {v: k for k, v in self._fwd.items()}

    def get_baseline(self, local_id):
        # story d6bd: baseline arbitration is always-on; unset -> None (local-wins).
        return None

    def is_pending(self, local_id):
        return False

    def get_jira_key(self, local_id: str) -> str | None:
        return self._fwd.get(local_id)

    def get_local_id(self, jira_key: str) -> str | None:
        return self._rev.get(jira_key)

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._fwd


# ---------------------------------------------------------------------------
# (a) outbound: existing (Blocks, target_key) → no re-ADD
# ---------------------------------------------------------------------------


def test_outbound_diff_links_skips_already_present(outbound_differ):
    """A local 'blocks' dep whose Jira issuelinks already carries the matching
    (Blocks, DIG-2) link → _diff_links returns [] (no re-emit / no churn)."""
    binding = StubBindingStore({"local-a": "DIG-1", "local-b": "DIG-2"})
    ticket = {
        "ticket_id": "local-a",
        "deps": [{"target_id": "local-b", "relation": "blocks", "link_uuid": "u-1"}],
    }
    # Jira side ALREADY has the Blocks link to DIG-2 (DIG-2 on the outward side).
    jira_fields = {
        "issuelinks": [
            {"type": {"name": "Blocks"}, "outwardIssue": {"key": "DIG-2"}},
        ]
    }

    out = outbound_differ._diff_links(ticket, jira_fields, binding)
    assert out == [], f"expected no re-ADD for an already-present link, got {out}"


def test_outbound_diff_links_emits_when_absent(outbound_differ):
    """Control: the SAME dep with NO matching Jira link emits exactly one add."""
    binding = StubBindingStore({"local-a": "DIG-1", "local-b": "DIG-2"})
    ticket = {
        "ticket_id": "local-a",
        "deps": [{"target_id": "local-b", "relation": "blocks", "link_uuid": "u-1"}],
    }
    out = outbound_differ._diff_links(ticket, {"issuelinks": []}, binding)
    assert len(out) == 1
    assert out[0]["type"] == "Blocks"
    assert out[0]["to_key"] == "DIG-2"


def test_outbound_diff_links_marks_swap_by_relation(outbound_differ):
    """The emitted ADD must carry the swap_endpoints flag so the applier writes the
    correct Jira direction (bug c8ed): depends_on -> (Blocks, swap=True), blocks ->
    (Blocks, swap=False). Previously the flag was discarded, so depends_on deps were
    written to Jira reversed."""
    binding = StubBindingStore({"local-a": "DIG-1", "local-b": "DIG-2"})

    def _emit(relation: str) -> dict:
        dep = {"target_id": "local-b", "relation": relation, "link_uuid": "u-1"}
        ticket = {"ticket_id": "local-a", "deps": [dep]}
        out = outbound_differ._diff_links(ticket, {"issuelinks": []}, binding)
        return out[0]

    assert _emit("depends_on").get("swap") is True, "depends_on must carry swap=True"
    assert _emit("blocks").get("swap") is False, "blocks must carry swap=False"


def test_outbound_diff_links_dedup_is_direction_agnostic(outbound_differ):
    """An existing Blocks link where DIG-2 is the INWARD side still suppresses
    the add (the dedup is direction-agnostic, per _existing_jira_links)."""
    binding = StubBindingStore({"local-a": "DIG-1", "local-b": "DIG-2"})
    ticket = {
        "ticket_id": "local-a",
        "deps": [{"target_id": "local-b", "relation": "blocks", "link_uuid": "u-1"}],
    }
    jira_fields = {"issuelinks": [{"type": {"name": "Blocks"}, "inwardIssue": {"key": "DIG-2"}}]}
    assert outbound_differ._diff_links(ticket, jira_fields, binding) == []


# ---------------------------------------------------------------------------
# (b) inbound: issuelink already in local deps → no churn
# ---------------------------------------------------------------------------


def test_inbound_diff_links_skips_already_present(inbound_differ):
    """A Jira issuelink already represented in the local ticket's deps →
    _diff_links_inbound returns [] (no churn)."""
    binding = StubBindingStore({"local-a": "DIG-1", "local-b": "DIG-2"})
    # Jira: DIG-1 has a Blocks link with DIG-2 on the inward side → X blocks Y
    # → rebar relation 'blocks' on local-a targeting local-b.
    jira_fields = {"issuelinks": [{"type": {"name": "Blocks"}, "inwardIssue": {"key": "DIG-2"}}]}
    local_ticket = {
        "ticket_id": "local-a",
        "deps": [{"target_id": "local-b", "relation": "blocks"}],
    }

    out = inbound_differ._diff_links_inbound(jira_fields, local_ticket, binding)
    assert out == [], f"expected no inbound churn for an already-present dep, got {out}"


def test_inbound_diff_links_emits_when_absent(inbound_differ):
    """Control: the SAME Jira link with NO matching local dep emits one add."""
    binding = StubBindingStore({"local-a": "DIG-1", "local-b": "DIG-2"})
    jira_fields = {"issuelinks": [{"type": {"name": "Blocks"}, "inwardIssue": {"key": "DIG-2"}}]}
    local_ticket = {"ticket_id": "local-a", "deps": []}

    out = inbound_differ._diff_links_inbound(jira_fields, local_ticket, binding)
    assert len(out) == 1
    assert out[0]["relation"] == "blocks"
    assert out[0]["target_id"] == "local-b"
