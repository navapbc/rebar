"""ABSOLUTE-direction coverage for inbound issue-link reflection, pinned to
CAPTURED LIVE-JIRA ground truth (bug 4b59 / epic 58b0 P2).

Why this file exists — the chronic-pattern guard:
    Every prior link-direction fix (3f04, c8ed, 3b86) was "proven" by a
    round-trip / self-consistency oracle ("outbound<->inbound AGREE", "a Blocks
    link exists in EITHER direction"). Such an oracle is INVARIANT UNDER A DOUBLE
    INVERSION, so two composed inversions (the outbound primitive + the inbound
    differ) cancel and every audit reads green while absolute direction is wrong.
    These tests deliberately assert ABSOLUTE direction against a real Jira payload
    whose ground truth is independently known, so an inverted mapping CANNOT pass.

Ground truth (captured 2026-07-17 from the live REB project, fixtures under
tests/fixtures/jira/live_ground_truth/):
    * 0250 (REB-458) BLOCKS ba7e (REB-461)  [local source of truth]
    * REB-458's issuelinks carry ``outwardIssue: REB-461`` with type.outward "blocks"
    * REB-461's issuelinks carry ``inwardIssue:  REB-458`` with type.inward "is blocked by"
  =>  outwardIssue + Blocks  MUST map to rebar ``blocks``
  =>  inwardIssue  + Blocks  MUST map to rebar ``depends_on``  (X is *blocked by* Y)
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
INBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "inbound_differ.py"
)
GROUND_TRUTH = REPO_ROOT / "tests" / "fixtures" / "jira" / "live_ground_truth"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def inbound_differ() -> ModuleType:
    return _load_module("inbound_differ", INBOUND_DIFFER_PATH)


class StubBindingStore:
    def __init__(self, bindings: dict[str, str]) -> None:
        self._rev = {v: k for k, v in bindings.items()}

    def get_local_id(self, jira_key: str) -> str | None:
        return self._rev.get(jira_key)


def _live_fields(key: str) -> dict:
    return json.loads((GROUND_TRUTH / f"{key}_issuelinks.json").read_text())["fields"]


# local_id <-> jira_key for the captured cluster
BINDINGS = {
    "0250": "REB-458",
    "ba7e": "REB-461",
    "98c6": "REB-460",
    "c6e5": "REB-462",
}


def test_outward_blocks_maps_to_blocks(inbound_differ):
    """REB-458 (0250) has ``outwardIssue: REB-461`` Blocks == '0250 blocks ba7e'.
    The inbound differ MUST map this to rebar relation ``blocks`` on 0250->ba7e."""
    fields = _live_fields("REB-458")  # 0250: outward-blocks -> REB-461 and REB-462
    local_ticket = {"ticket_id": "0250", "deps": []}
    out = inbound_differ._diff_links_inbound(fields, local_ticket, StubBindingStore(BINDINGS))
    by_target = {m["target_id"]: m["relation"] for m in out}
    assert by_target.get("ba7e") == "blocks", (
        f"outwardIssue Blocks must map to 'blocks' (0250 blocks ba7e); got {by_target}"
    )
    assert by_target.get("c6e5") == "blocks", by_target


def test_inward_blocks_maps_to_depends_on(inbound_differ):
    """REB-461 (ba7e) has ``inwardIssue: REB-458`` Blocks == 'ba7e is blocked by 0250'
    == 'ba7e depends_on 0250'. The inbound differ MUST map this to ``depends_on``."""
    fields = _live_fields("REB-461")  # ba7e: inward-blocked-by REB-458, outward-blocks REB-460
    local_ticket = {"ticket_id": "ba7e", "deps": []}
    out = inbound_differ._diff_links_inbound(fields, local_ticket, StubBindingStore(BINDINGS))
    by_target = {m["target_id"]: m["relation"] for m in out}
    assert by_target.get("0250") == "depends_on", (
        f"inwardIssue Blocks must map to 'depends_on' (ba7e is blocked by 0250); got {by_target}"
    )
    # ba7e's OUTWARD link to 98c6 is 'ba7e blocks 98c6'
    assert by_target.get("98c6") == "blocks", by_target


# --- Inverse-aware cross-ticket dedup + active-set guard (bug 4b59) --------------
# rebar stores each blocking edge ONCE, one-directionally. The same Jira link is
# visible from BOTH endpoints, so the inbound differ must NOT re-emit the edge the
# counterpart already owns in its inverse form, and must NOT mirror a link onto a
# ticket outside the active local set. Live-validated to converge to 0 on the real
# REB project (see the rebar-debug session log).

_INWARD_BLOCK = {"issuelinks": [{"type": {"name": "Blocks"}, "inwardIssue": {"key": "DIG-Y"}}]}
_OUTWARD_BLOCK = {"issuelinks": [{"type": {"name": "Blocks"}, "outwardIssue": {"key": "DIG-Y"}}]}
_BIND = {"local-x": "DIG-X", "local-y": "DIG-Y"}


def test_inverse_aware_dedup_counterpart_owns_edge(inbound_differ):
    """X's inward Blocks<-Y == 'X depends_on Y'. If the COUNTERPART Y already owns the
    inverse edge ('Y blocks X'), the differ must emit NOTHING (no mirror-add)."""
    x = {"ticket_id": "local-x", "deps": []}
    y = {"ticket_id": "local-y", "deps": [{"relation": "blocks", "target_id": "local-x"}]}
    out = inbound_differ._diff_links_inbound(
        _INWARD_BLOCK, x, StubBindingStore(_BIND), {"local-x": x, "local-y": y}
    )
    assert out == [], f"counterpart owns 'Y blocks X'; must not mirror 'X depends_on Y', got {out}"


def test_archived_or_absent_counterpart_skipped(inbound_differ):
    """When the counterpart is NOT in the active local map (archived/deleted/unbound),
    the differ must skip rather than mirror onto a dormant ticket."""
    x = {"ticket_id": "local-x", "deps": []}
    out = inbound_differ._diff_links_inbound(
        _OUTWARD_BLOCK,
        x,
        StubBindingStore(_BIND),
        {"local-x": x},  # local-y absent
    )
    assert out == [], f"counterpart absent from active set; must skip, got {out}"


def test_genuine_new_jira_link_is_adopted(inbound_differ):
    """A genuinely-new Jira link (X outward Blocks-> Y) to an ACTIVE counterpart that
    does NOT already own the edge IS adopted, with the correct absolute direction."""
    x = {"ticket_id": "local-x", "deps": []}
    y = {"ticket_id": "local-y", "deps": []}
    out = inbound_differ._diff_links_inbound(
        _OUTWARD_BLOCK, x, StubBindingStore(_BIND), {"local-x": x, "local-y": y}
    )
    assert out == [{"action": "add", "target_id": "local-y", "relation": "blocks"}], out
