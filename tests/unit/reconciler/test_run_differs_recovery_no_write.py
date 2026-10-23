"""Preview and dry-run differ passes must perform no remote recovery writes.

Pending-binding recovery runs only for an unscoped pass with persist enabled.
Keyed-pending recovery still writes identity on a normal pass. BindingRecovery
tests separately guard keyed no-search and keyless absence thresholds."""

from __future__ import annotations

import json
import types
from pathlib import Path

from rebar.reconciler import local_label_intent, run_differs
from rebar.reconciler.binding_store import BindingStore


class _SpyTransport:
    """Records every REMOTE identity write recovery would issue."""

    def __init__(self) -> None:
        self.writes: list[str] = []

    # The three members recover_pending_bindings requires of its client.
    def add_label(self, key: str, label: str) -> None:
        self.writes.append(f"add_label({key},{label})")

    def set_entity_property(self, key: str, prop: str, value: object) -> None:
        self.writes.append(f"set_entity_property({key},{prop})")

    def search_issues(self, jql: str, **_kw: object) -> list[dict]:
        # Keyed-pending recovery never searches; a search here would itself be a
        # (read) contract break, so record it too.
        self.writes.append(f"search_issues({jql})")
        return []


def _seed_keyed_pending(tracker: Path, local_id: str = "loc-A", jira_key: str = "DIG-A") -> None:
    """Seed a keyed-pending binding in the store for recovery checks."""
    bridge = tracker / ".bridge_state"
    bridge.mkdir(parents=True, exist_ok=True)
    doc = {
        "version": 2,
        "bindings": {
            local_id: {
                "jira_key": jira_key,
                "state": "pending",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        },
        "reverse": {},
        "comment_ids": {},
    }
    (bridge / "bindings.json").write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _make_ctx(tmp_path: Path, *, persist: bool) -> tuple[types.SimpleNamespace, _SpyTransport]:
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir(parents=True, exist_ok=True)
    _seed_keyed_pending(tracker)
    spy = _SpyTransport()
    # Isolate the recovery gate: stub the differ compute so only recovery + the
    # gate execute (the outbound differ itself is covered elsewhere).
    stub_differ = types.SimpleNamespace(
        compute_outbound_mutations=lambda *a, **k: ([], {}),
        OutboundDiffConfig=lambda **k: types.SimpleNamespace(**k),
    )
    ctx = types.SimpleNamespace(
        persist=persist,
        filter_local_ids=None,
        selection_ids=None,
        binding_store=BindingStore(tracker),
        local_tickets=[],
        local_label_intent_mod=local_label_intent,
        tracker_dir=tracker,
        repo_root=tmp_path,
        outbound_differ_mod=stub_differ,
        pass_id="p-7851",
        prev_snapshot={},
        curr_snapshot={},
        sync_logger=types.SimpleNamespace(log=lambda *a, **k: None),
        recovery_failures=0,
    )
    return ctx, spy


def test_no_write_pass_performs_zero_remote_recovery_writes(tmp_path):
    """A no-write (persist=False) unscoped pass with a keyed-pending binding must
    issue ZERO remote add_label / set_entity_property from recovery."""
    ctx, spy = _make_ctx(tmp_path, persist=False)
    backend = types.SimpleNamespace(transport=spy, outbound=None, inbound=None)

    run_differs._run_differs_outbound(ctx, [], backend)

    assert spy.writes == [], (
        f"a no-write pass must issue zero remote recovery writes, got: {spy.writes}"
    )


def test_live_pass_still_performs_recovery_writes(tmp_path):
    """An unscoped persisted pass writes identity during keyed recovery."""
    ctx, spy = _make_ctx(tmp_path, persist=True)
    backend = types.SimpleNamespace(transport=spy, outbound=None, inbound=None)

    run_differs._run_differs_outbound(ctx, [], backend)

    assert spy.writes == [
        "add_label(DIG-A,rebar-id:loc-A)",
        "set_entity_property(DIG-A,local_id)",
    ], f"a live pass must recover the keyed-pending binding, got: {spy.writes}"
