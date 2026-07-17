"""Managed-reference provenance: the compaction-surviving removal-sync primitive.

Covers story safe-luge-nog's foundational layer:
  - the reducer maintains a strictly-monotonic ``managed_refs`` projection
    (folded from CREATE/LINK/parent-EDIT, never reduced by UNLINK/detach),
  - it survives a compaction boundary (a removal still propagates after compact),
  - an old SNAPSHOT lacking the field is migration-seeded from current refs,
  - the shared ``should_propagate_removal`` gate decides REMOVE-vs-ADOPT.

The reducer-behaviour tests drive the REAL ``reduce_ticket`` replay path (not the
processors in isolation) so the dispatch wiring + snapshot interaction is exercised.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from _events import _UUID, _UUID2, _UUID3, _write_event

from rebar.reducer._managed_refs import (
    seed_managed_refs_from_current,
    should_propagate_removal,
)

pytestmark = [pytest.mark.unit, pytest.mark.scripts]

_OUTBOUND_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "rebar"
    / "_engine"
    / "rebar_reconciler"
    / "outbound_differ.py"
)


@pytest.fixture(scope="module")
def outbound_differ() -> ModuleType:
    spec = importlib.util.spec_from_file_location("outbound_differ_mr", _OUTBOUND_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("outbound_differ_mr", mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class _Store:
    def __init__(self, bindings: dict[str, str]) -> None:
        self._b = bindings  # {local_id: jira_key}

    def get_baseline(self, local_id):
        # story d6bd: baseline arbitration is always-on; unset -> None (local-wins).
        return None

    def is_pending(self, local_id):
        return False

    def get_jira_key(self, local_id: str) -> str | None:
        return self._b.get(local_id)

    def get_local_id(self, jira_key: str) -> str | None:
        return next((lid for lid, k in self._b.items() if k == jira_key), None)

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._b


def _refs(state: dict) -> set[tuple[str, str]]:
    return {(k, t) for k, t in state.get("managed_refs", [])}


# ── reducer: monotonic fold ─────────────────────────────────────────────────
def test_link_then_unlink_keeps_managed_ref(tmp_path: Path, reducer: ModuleType) -> None:
    """A LINK folds (relation, target) into managed_refs; the later UNLINK removes the
    dep but NOT the managed ref — that monotonicity is what lets the unlink propagate a
    peer delete instead of being silently re-resurrected."""
    d = tmp_path / "abc-1111-2222-3333"
    d.mkdir()
    _write_event(d, 1, _UUID, "CREATE", {"ticket_type": "task", "title": "T"})
    _write_event(d, 2, _UUID2, "LINK", {"target_id": "tgt-4444", "relation": "blocks"})
    _write_event(d, 3, _UUID3, "UNLINK", {"link_uuid": _UUID2})
    state = reducer.reduce_ticket(d)

    assert state["deps"] == [], "the dep is gone (UNLINK applied)"
    assert ("blocks", "tgt-4444") in _refs(state), "but the managed ref survives (monotonic)"


def test_create_with_parent_and_reparent_fold(tmp_path: Path, reducer: ModuleType) -> None:
    """A parent set at CREATE and a later re-parent EDIT both fold; a detach removes
    neither (monotonic), so a detach of a managed parent can later propagate a clear."""
    d = tmp_path / "abc-5555-6666-7777"
    d.mkdir()
    _write_event(
        d, 1, _UUID, "CREATE", {"ticket_type": "task", "title": "T", "parent_id": "epic-a"}
    )
    _write_event(d, 2, _UUID2, "EDIT", {"fields": {"parent_id": "epic-b"}})
    _write_event(d, 3, _UUID3, "EDIT", {"fields": {"parent_id": None}})  # detach
    state = reducer.reduce_ticket(d)

    assert state["parent_id"] is None, "currently detached"
    assert ("parent", "epic-a") in _refs(state)
    assert ("parent", "epic-b") in _refs(state), "both managed parents retained (monotonic)"


def test_never_referenced_ticket_has_empty_managed_refs(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """A ticket that never had a parent or link carries an empty managed_refs — so the
    gate proposes NO removals for it (no churn the other direction)."""
    d = tmp_path / "abc-8888-9999-0000"
    d.mkdir()
    _write_event(d, 1, _UUID, "CREATE", {"ticket_type": "task", "title": "T"})
    state = reducer.reduce_ticket(d)
    assert state["managed_refs"] == []


# ── reducer: idempotent under duplicate-delivered events ────────────────────
def test_managed_ref_fold_is_idempotent(tmp_path: Path, reducer: ModuleType) -> None:
    """Re-folding the same logical ref (duplicate parent EDIT) does not duplicate it."""
    d = tmp_path / "abc-aaaa-bbbb-cccc"
    d.mkdir()
    _write_event(d, 1, _UUID, "CREATE", {"ticket_type": "task", "title": "T"})
    _write_event(d, 2, _UUID2, "EDIT", {"fields": {"parent_id": "epic-x"}})
    _write_event(d, 3, _UUID3, "EDIT", {"fields": {"parent_id": "epic-x"}})
    state = reducer.reduce_ticket(d)
    assert state["managed_refs"].count(["parent", "epic-x"]) == 1


# ── migration seed (pre-feature / old SNAPSHOT lacking the field) ───────────
def test_snapshot_without_managed_refs_is_seeded_from_current(
    tmp_path: Path, reducer: ModuleType
) -> None:
    """An old SNAPSHOT whose compiled_state predates managed_refs is seeded from the
    restored current parent_id + deps, so the ticket's existing refs become managed and
    a subsequent unlink/detach can propagate (closing the compaction durability hole)."""
    d = tmp_path / "abc-dddd-eeee-ffff"
    d.mkdir()
    legacy_compiled = {
        "ticket_id": "abc-dddd-eeee-ffff",
        "ticket_type": "task",
        "title": "T",
        "status": "open",
        "parent_id": "epic-legacy",
        "deps": [{"target_id": "tgt-legacy", "relation": "relates_to", "link_uuid": _UUID2}],
        # NOTE: deliberately NO "managed_refs" key — simulates a pre-feature snapshot.
    }
    _write_event(d, 10, _UUID, "SNAPSHOT", {"compiled_state": legacy_compiled})
    state = reducer.reduce_ticket(d)
    assert ("parent", "epic-legacy") in _refs(state)
    assert ("relates_to", "tgt-legacy") in _refs(state)


def test_seed_helper_builds_from_parent_and_deps() -> None:
    state = {
        "parent_id": "epic-1",
        "deps": [
            {"target_id": "t1", "relation": "blocks", "link_uuid": "u1"},
            {"target_id": "t2", "relation": "depends_on", "link_uuid": "u2"},
            {"target_id": "t3", "relation": "duplicates", "link_uuid": "u3"},  # unmanaged kind
        ],
    }
    seeded = {tuple(p) for p in seed_managed_refs_from_current(state)}
    assert seeded == {("parent", "epic-1"), ("blocks", "t1"), ("depends_on", "t2")}


# ── compaction survival (post-feature SNAPSHOT carries + round-trips) ───────
def test_managed_refs_survive_compaction_roundtrip(tmp_path: Path, reducer: ModuleType) -> None:
    """Reducing a fresh log yields managed_refs; feeding that compiled_state back as a
    SNAPSHOT (what compact_ticket persists) restores the SAME refs verbatim — the field
    survives a compaction boundary, so a removal still propagates afterwards."""
    d = tmp_path / "abc-1212-3434-5656"
    d.mkdir()
    _write_event(
        d, 1, _UUID, "CREATE", {"ticket_type": "task", "title": "T", "parent_id": "epic-z"}
    )
    _write_event(d, 2, _UUID2, "LINK", {"target_id": "tgt-9", "relation": "blocks"})
    pre = reducer.reduce_ticket(d)
    pre_refs = _refs(pre)
    assert ("parent", "epic-z") in pre_refs and ("blocks", "tgt-9") in pre_refs

    # Simulate compaction: persist a SNAPSHOT from the reduced compiled_state.
    d2 = tmp_path / "abc-1212-3434-5657"
    d2.mkdir()
    _write_event(d2, 10, _UUID3, "SNAPSHOT", {"compiled_state": dict(pre)})
    post = reducer.reduce_ticket(d2)
    assert _refs(post) == pre_refs, "managed_refs restored verbatim across the snapshot"


# ── the shared gate ─────────────────────────────────────────────────────────
def test_gate_propagates_only_managed_refs() -> None:
    ticket = {"managed_refs": [["blocks", "tgt-1"], ["parent", "epic-1"]]}
    # Managed ref absent locally -> propagate the peer delete.
    assert should_propagate_removal("blocks", "tgt-1", ticket) is True
    assert should_propagate_removal("parent", "epic-1", ticket) is True
    # A never-managed peer ref -> do NOT delete (adopt inbound instead).
    assert should_propagate_removal("blocks", "tgt-human", ticket) is False
    assert should_propagate_removal("relates_to", "tgt-1", ticket) is False


def test_gate_defaults_missing_managed_refs_to_no_removal() -> None:
    """Absent/empty managed_refs degrades to additive-only: the gate never proposes a
    delete, so a transient/absent projection can only delay convergence (fail-open)."""
    assert should_propagate_removal("blocks", "tgt-1", {}) is False
    assert should_propagate_removal("parent", "epic-1", {"managed_refs": []}) is False
    assert should_propagate_removal("blocks", "tgt-1", {"managed_refs": "garbage"}) is False


# ── compaction-boundary end-to-end: compact → post-compaction UNLINK → differ removes ──
def test_unlink_after_compaction_still_propagates_removal(
    tmp_path: Path, reducer: ModuleType, outbound_differ: ModuleType
) -> None:
    """The durability hole closed (story safe-luge-nog AC3): a removal performed AFTER a
    compaction boundary still propagates. Chain it end-to-end — reduce a CREATE+LINK, COMPACT
    to a SNAPSHOT (managed_refs lives in compiled_state), then a POST-compaction UNLINK, reduce
    again (deps empty, managed_refs survives), and feed the reduced ticket to the outbound
    differ: it must emit the link REMOVE. A raw-event ever-seen projection would fail closed
    here (the compacted log no longer proves we managed the link) and re-resurrect it."""
    # 1) reduce a real CREATE + LINK to get the pre-compaction compiled_state.
    d1 = tmp_path / "local-1"
    d1.mkdir()
    _write_event(d1, 1, _UUID, "CREATE", {"ticket_type": "task", "title": "T"})
    _write_event(d1, 2, _UUID2, "LINK", {"target_id": "local-2", "relation": "blocks"})
    pre = reducer.reduce_ticket(d1)
    assert ("blocks", "local-2") in {(k, t) for k, t in pre["managed_refs"]}

    # 2) COMPACT: a fresh dir whose only history is a SNAPSHOT of that compiled_state, then a
    #    POST-compaction UNLINK of the link (by its link_uuid == the LINK event uuid).
    d2 = tmp_path / "local-1b"
    d2.mkdir()
    _write_event(d2, 10, _UUID3, "SNAPSHOT", {"compiled_state": dict(pre)})
    _write_event(d2, 11, "u4444444-0000-0000-0000-000000000004", "UNLINK", {"link_uuid": _UUID2})
    post = reducer.reduce_ticket(d2)
    assert post["deps"] == [], "the post-compaction UNLINK removed the dep"
    assert ("blocks", "local-2") in {(k, t) for k, t in post["managed_refs"]}, (
        "managed_refs survived compaction (monotonic) — the durability hole is closed"
    )

    # 3) RECONCILE: the outbound differ over a Jira snapshot still carrying the link must emit
    #    the REMOVE, driven by the compaction-surviving managed_refs.
    store = _Store({"local-1": "PROJ-1", "local-2": "PROJ-2"})
    snapshot = {
        "PROJ-1": {
            "summary": "T",
            "description": "",
            "issuetype": "Task",
            "priority": "Medium",
            "status": "To Do",
            "assignee": "",
            "labels": [],
            "issuelinks": [
                # LIVE-JIRA direction (bug 4b59): 'local-1 blocks local-2' is an OUTWARD
                # Blocks on local-1's issue (was inwardIssue under the reversed convention).
                {"id": "L1", "type": {"name": "Blocks"}, "outwardIssue": {"key": "PROJ-2"}}
            ],
        }
    }
    outs, _ = outbound_differ.compute_outbound_mutations(
        local_tickets=[post], jira_snapshot=snapshot, binding_store=store
    )
    removes = [
        lk
        for m in outs
        for lk in (m.links or [])
        if lk.get("action") == "remove" and lk.get("to_key") == "PROJ-2"
    ]
    assert removes, (
        "a managed link unlinked AFTER compaction must still emit an outbound REMOVE — "
        f"the removal propagates across the compaction boundary; got {outs}"
    )
