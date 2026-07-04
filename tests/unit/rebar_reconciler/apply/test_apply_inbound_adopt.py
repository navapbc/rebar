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
    assert baseline["status"] == {"name": "To Do"}
    assert baseline["priority"] == {"name": "High"}


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
