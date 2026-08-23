"""ADOPT contract at the inbound-create leaf (epic 3006-e198 / ticket 5854).

Two guards were added to ``_apply_inbound_create`` for the class-B adopt path:

* **Gate #1 — retired-skip (ADR 0027 §4a):** a RETIRED key (its binding was GC'd
  by class C) must never be re-adopted (no delete/re-adopt loop). The leaf returns
  early with ``skipped_retired`` and writes NO local ticket.
* **Gate #4 — baseline seed (ADR 0027 §4c / ADR 0029 §3):** on a real adopt, the
  per-binding baseline is seeded from the adopted Jira fields immediately after
  bind, so the FIRST outbound diff is empty (echo suppression).

Uses ``client=None`` (the Jira label write-back is guarded), so the leaf runs
purely against a real BindingStore + a tmp tracker dir.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[4] / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SRC / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_apply_inbound = _load("_apply_inbound_adopt_ut", "apply_inbound.py")
_mutation = _load("_mutation_adopt_ut", "mutation.py")
_bs = _load("_binding_store_adopt_ut", "binding_store.py")
_walk = _load("_binding_walk_adopt_ut", "binding_walk.py")
_classify = _load("_classify_adopt_ut", "classify.py")
_ob = _load("_ob_adopt_ut", "outbound_differ.py")
BindingStore = _bs.BindingStore


def _adopt_mutation(jira_key: str, fields: dict):
    return _mutation.Mutation(
        direction=_mutation.MutationDirection.inbound,
        action=_mutation.MutationAction.create,
        target=jira_key,
        payload={"fields": fields, "jira_fields": fields},
        provenance={"source": "binding_walk", "drift_class": "B", "jira_key": jira_key},
    )


def test_adopt_seeds_baseline_from_jira_fields(tmp_path: Path) -> None:
    """A real adopt binds the deterministic local id AND seeds the baseline from
    the adopted Jira fields (the 5 mirrored fields), so the next outbound diff is
    empty."""
    bs = BindingStore(tmp_path / ".tickets-tracker")
    fields = {
        "summary": "native issue",
        "description": "a body",
        "priority": {"name": "High"},
        "status": {"name": "To Do"},
        "assignee": {"displayName": "Someone"},
    }
    mutation = _adopt_mutation("REB-532", fields)
    result = _apply_inbound._apply_inbound_create(
        mutation, client=None, repo_root=tmp_path, binding_store=bs
    )
    local_id = _apply_inbound._jira_key_to_local_id("REB-532")
    # Bound to the deterministic local id.
    assert bs.get_jira_key(local_id) == "REB-532"
    assert result.payload.get("dedup_skipped") is not True
    # Baseline seeded with the mirrored fields (echo suppression).
    baseline = bs.get_baseline(local_id)
    assert baseline is not None
    assert baseline["summary"] == "native issue"
    assert baseline["description"] == "a body"
    assert baseline["status"] == "To Do"
    assert baseline["priority"] == "High"
    assert baseline["assignee"] == {"displayName": "Someone"}


def test_adopt_skips_a_retired_key(tmp_path: Path) -> None:
    """A retired key must not be resurrected — the leaf returns skipped_retired and
    creates no local ticket / binding."""
    bs = BindingStore(tmp_path / ".tickets-tracker")
    # Retire REB-530 via GRACE consecutive 404s (the class-C GC path).
    bs.bind_confirm("loc-old", "REB-530")
    for _ in range(3):
        bs.note_absent("REB-530")
    assert bs.is_retired("REB-530")

    mutation = _adopt_mutation("REB-530", {"summary": "resurrected?", "status": {"name": "To Do"}})
    result = _apply_inbound._apply_inbound_create(
        mutation, client=None, repo_root=tmp_path, binding_store=bs
    )
    assert result.payload.get("skipped_retired") is True
    # No new binding for the retired key's deterministic local id.
    local_id = _apply_inbound._jira_key_to_local_id("REB-530")
    assert bs.get_jira_key(local_id) is None


class _StampingClient:
    """The three transport methods the inbound-create write-back touches."""

    def __init__(self) -> None:
        self.labels: list[tuple[str, str]] = []

    def add_label(self, key: str, label: str) -> None:
        self.labels.append((key, label))

    def set_entity_property(self, key: str, name: str, value: str) -> None:
        pass

    def get_comments(self, key: str) -> list:
        return []


def test_lost_final_push_runner_replay_heals(tmp_path: Path) -> None:
    """Bug 2392-9389-39f9-4ca6 end-to-end: the lost-final-push ephemeral runner.

    Runner 1 adopts a Jira-native issue — the CREATE event + binding are written
    and the ``rebar-id:<deterministic-id>`` label is stamped on Jira — then the
    pass's final tickets push is REJECTED (non-fast-forward) and the runner is
    destroyed: ALL runner-1 local state is discarded while the Jira label
    persists. Runner 2 starts from a fresh store + fresh tracker and sees only
    the labeled snapshot. The walk must emit the replay adopt, and applying it
    through the SAME inbound-create leaf re-materialises the local ticket +
    binding under the SAME deterministic local id (the manual REB-3510 repair,
    mechanized)."""
    fields = {
        "summary": "native issue",
        "status": {"name": "To Do"},
        "priority": {"name": "High"},
    }

    # ── runner 1: the adopt happens; the label is stamped on the Jira side ──
    runner1 = tmp_path / "runner1"
    bs1 = BindingStore(runner1 / ".tickets-tracker")
    client1 = _StampingClient()
    result1 = _apply_inbound._apply_inbound_create(
        _adopt_mutation("REB-3510", fields), client=client1, repo_root=runner1, binding_store=bs1
    )
    local_id = _apply_inbound._jira_key_to_local_id("REB-3510")
    assert bs1.get_jira_key(local_id) == "REB-3510"
    assert result1.payload.get("dedup_skipped") is not True
    stamped = [lbl for (_k, lbl) in client1.labels if lbl.startswith("rebar-id")]
    assert stamped == [f"rebar-id:{local_id}"], "runner 1 stamped the identity label"
    # …then the final tickets push is rejected and the ephemeral runner is
    # destroyed: bs1/runner1 are never consulted again (local state lost); only
    # the stamped label survives, riding the next pass's snapshot.

    # ── runner 2: fresh store + tracker; the snapshot carries the label ──
    runner2 = tmp_path / "runner2"
    bs2 = BindingStore(runner2 / ".tickets-tracker")
    snapshot_fields = dict(fields)
    snapshot_fields["labels"] = [f"rebar-id:{local_id}"]
    walk = _walk.compute_binding_walk_mutations(
        bs2,
        {"REB-3510": snapshot_fields},
        set(),
        client=None,
        local_reader=lambda _lid: None,  # the local CREATE died with runner 1
        max_acting_fraction=1.0,
        classify_mod=_classify,
        mutation_mod=_mutation,
        outbound_differ_mod=_ob,
    )
    assert walk.adopted == ["REB-3510"], "runner 2 must replay the lost adopt"
    assert len(walk.mutations) == 1

    result2 = _apply_inbound._apply_inbound_create(
        walk.mutations[0], client=None, repo_root=runner2, binding_store=bs2
    )
    assert result2.payload.get("dedup_skipped") is not True
    # Healed: same deterministic local id, binding restored, baseline seeded.
    assert result2.payload.get("local_id") == local_id
    assert bs2.get_jira_key(local_id) == "REB-3510"
    assert bs2.get_baseline(local_id) is not None
