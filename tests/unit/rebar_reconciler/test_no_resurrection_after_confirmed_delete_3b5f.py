"""Bug 3b5f: a confirmed-deleted Jira pairing is TOMBSTONED, never resurrected.

Operator ruling (recorded on the ticket): once an issue is deleted in Jira it must
not be re-created. Before this change rebar resurrected it on a delay — three
consecutive confirmed-404 ``note_absent`` calls retire the binding, ``_retire``
pops the entry from BOTH ``bindings`` and ``reverse`` so ``get_jira_key`` returns
``None``, and the unbound-create arm in ``outbound_differ`` then emits an outbound
CREATE for the now-unbound local ticket. A fresh Jira issue replaced the
deliberately-deleted one about three passes later.

The guard SUPPRESSES work, so its failure mode is silently not creating issues —
which no green suite would surface. Hence both halves are asserted here in one
module: the retired pairing must NOT create, and a NEVER-BOUND local ticket MUST
still create. The tombstone is also reversible by a documented call
(``BindingStore.unretire``), and the suppression is observable in bridge_alerts.

Follows the importlib loader convention established across this test tree.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_REC = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load_module(name: str, path: Path) -> ModuleType:
    if name in sys.modules:
        del sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def od() -> ModuleType:
    return _load_module("outbound_differ_3b5f", _REC / "outbound_differ.py")


@pytest.fixture()
def binding_store_mod() -> ModuleType:
    return _load_module("binding_store_3b5f", _REC / "binding_store.py")


def _ticket(tid: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ticket_id": tid,
        "title": "Some issue",
        "description": "desc",
        "status": "open",
        "priority": 2,
        "ticket_type": "bug",
        "assignee": "",
        "tags": [],
        "comments": [],
        "deps": [],
    }
    base.update(over)
    return base


def _retired_store(binding_store_mod: ModuleType, repo_root: Path, local_id: str, jira_key: str):
    """A real BindingStore whose ``local_id``↔``jira_key`` pairing has been retired.

    Retirement is driven the production way — repeated confirmed-404 ``note_absent``
    calls to GRACE — not by writing bindings-retired.json by hand.
    """
    store = binding_store_mod.BindingStore(repo_root / ".tickets-tracker")
    store.bind_confirm(local_id, jira_key)
    store.save()
    for _ in range(binding_store_mod._DEFAULT_ABSENT_RETIRE_GRACE):
        store.note_absent(jira_key)
    assert store.is_retired(jira_key), "precondition: the pairing must be retired"
    assert store.get_jira_key(local_id) is None, "precondition: local must be unbound"
    return store


def _creates(mutations: list[Any]) -> list[Any]:
    return [m for m in mutations if getattr(m, "action", "") == "create"]


def _alert_records(repo_root: Path) -> list[dict[str, Any]]:
    alerts_dir = repo_root / "bridge_state" / "bridge_alerts"
    records: list[dict[str, Any]] = []
    for path in sorted(alerts_dir.glob("*.jsonl")) if alerts_dir.exists() else ():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


# ===========================================================================
# The tombstone: a confirmed-deleted pairing is never re-created
# ===========================================================================


def test_a_retired_pairing_gets_no_outbound_create(
    od: ModuleType, binding_store_mod: ModuleType, tmp_path: Path
) -> None:
    """The core suppression: retired (confirmed 404) ⇒ NO outbound create."""
    store = _retired_store(binding_store_mod, tmp_path, "loc-1", "DIG-1")

    mutations, _ = od.compute_outbound_mutations(
        local_tickets=[_ticket("loc-1")],
        jira_snapshot={},
        binding_store=store,
        config=od.OutboundDiffConfig(pass_id="p1"),
    )

    assert _creates(mutations) == [], (
        "A local ticket whose binding was retired on a confirmed 404 must NOT be "
        "re-created in Jira (operator ruling: a deleted issue is never resurrected); "
        f"got {[(m.local_id, m.action) for m in mutations]}"
    )


def test_a_never_bound_local_ticket_still_gets_its_outbound_create(
    od: ModuleType, binding_store_mod: ModuleType, tmp_path: Path
) -> None:
    """The guard must not suppress ordinary creation — that distinction is the point."""
    store = _retired_store(binding_store_mod, tmp_path, "loc-1", "DIG-1")

    mutations, _ = od.compute_outbound_mutations(
        local_tickets=[_ticket("loc-1"), _ticket("loc-fresh")],
        jira_snapshot={},
        binding_store=store,
        config=od.OutboundDiffConfig(pass_id="p1"),
    )

    created = sorted(m.local_id for m in _creates(mutations))
    assert created == ["loc-fresh"], (
        f"A NEVER-BOUND local ticket has no tombstone and must still be created; created={created}"
    )


def test_the_suppression_is_observable_and_names_the_unretire_route(
    od: ModuleType, binding_store_mod: ModuleType, tmp_path: Path
) -> None:
    """A suppressed create is loud: local id + retired key + the way back."""
    store = _retired_store(binding_store_mod, tmp_path, "loc-1", "DIG-1")

    od.compute_outbound_mutations(
        local_tickets=[_ticket("loc-1")],
        jira_snapshot={},
        binding_store=store,
        config=od.OutboundDiffConfig(pass_id="p1"),
    )

    suppressions = [
        r for r in _alert_records(tmp_path) if r.get("kind") == "outbound-create-suppressed"
    ]
    assert suppressions, (
        "Suppressing a create must be observable — no bridge alert was written, so "
        "the tombstone would silently stop syncing a ticket."
    )
    record = suppressions[0]
    assert record.get("local_id") == "loc-1"
    assert record.get("jira_key") == "DIG-1"
    assert "unretire" in json.dumps(record), (
        "The alert must name BindingStore.unretire(<jira_key>) as the route back to "
        f"creation; got {record!r}"
    )


# ===========================================================================
# The escape hatch: unretire(jira_key) makes the ticket creatable again
# ===========================================================================


def test_unretire_drops_the_key_from_the_retired_set_and_file(
    binding_store_mod: ModuleType, tmp_path: Path
) -> None:
    store = _retired_store(binding_store_mod, tmp_path, "loc-1", "DIG-1")
    retired_path = tmp_path / ".tickets-tracker" / ".bridge_state" / "bindings-retired.json"
    assert "DIG-1" in json.loads(retired_path.read_text())["retired"]

    assert store.unretire("DIG-1") is True
    assert not store.is_retired("DIG-1")
    assert "DIG-1" not in json.loads(retired_path.read_text())["retired"]
    # Idempotent: un-retiring an unknown key is a no-op, not an error.
    assert store.unretire("DIG-1") is False


def test_unretire_lets_the_next_pass_create_the_ticket_again(
    od: ModuleType, binding_store_mod: ModuleType, tmp_path: Path
) -> None:
    """The tombstone is reversible by a documented call, not by hand-editing state."""
    store = _retired_store(binding_store_mod, tmp_path, "loc-1", "DIG-1")
    store.unretire("DIG-1")

    mutations, _ = od.compute_outbound_mutations(
        local_tickets=[_ticket("loc-1")],
        jira_snapshot={},
        binding_store=store,
        config=od.OutboundDiffConfig(pass_id="p2"),
    )

    assert [m.local_id for m in _creates(mutations)] == ["loc-1"], (
        "After unretire the local ticket is ordinary unbound work again and the next "
        "pass must create it."
    )


def test_is_retired_local_reverse_lookup(binding_store_mod: ModuleType, tmp_path: Path) -> None:
    store = _retired_store(binding_store_mod, tmp_path, "loc-1", "DIG-1")
    assert store.is_retired_local("loc-1") is True
    assert store.is_retired_local("loc-never-bound") is False
    store.unretire("DIG-1")
    assert store.is_retired_local("loc-1") is False


# ===========================================================================
# Legacy/duck-typed store tolerance (the way outbound_differ._is_retired does it)
# ===========================================================================


class _LegacyStubStore:
    """A duck-typed store from before 3b5f: no retired-local accessor at all."""

    def get_baseline(self, local_id: str) -> None:
        return None

    def is_pending(self, local_id: str) -> bool:
        return False

    def get_jira_key(self, local_id: str) -> str | None:
        return None

    def is_bound(self, local_id: str) -> bool:
        return False


def test_a_legacy_store_lacking_the_accessor_still_creates(od: ModuleType, tmp_path: Path) -> None:
    """Fail-OPEN: a store without the tombstone accessor must not lose its creates."""
    mutations, _ = od.compute_outbound_mutations(
        local_tickets=[_ticket("loc-1")],
        jira_snapshot={},
        binding_store=_LegacyStubStore(),
        config=od.OutboundDiffConfig(pass_id="p1"),
    )
    assert [m.local_id for m in _creates(mutations)] == ["loc-1"], (
        "The tombstone lookup must degrade the way outbound_differ._is_retired does — "
        "a legacy/duck-typed store lacking the accessor creates as before."
    )


class _RaisingStubStore(_LegacyStubStore):
    def is_retired_local(self, local_id: str) -> bool:
        raise RuntimeError("legacy store blew up")

    def retired_key_for_local(self, local_id: str) -> str:
        raise RuntimeError("legacy store blew up")


def test_a_raising_accessor_fails_open_and_still_creates(od: ModuleType, tmp_path: Path) -> None:
    mutations, _ = od.compute_outbound_mutations(
        local_tickets=[_ticket("loc-1")],
        jira_snapshot={},
        binding_store=_RaisingStubStore(),
        config=od.OutboundDiffConfig(pass_id="p1"),
    )
    assert [m.local_id for m in _creates(mutations)] == ["loc-1"]


# ===========================================================================
# The WIRE trap: the snapshot-differ suppression must STAY
# ===========================================================================


def test_snapshot_differ_local_state_suppression_is_still_present_and_consulted() -> None:
    """``drop_snapshot_differ_local_state_emissions`` must not be removed here.

    The ticket's "what is NOT the fix" warning is about the WIRE option — migrating
    ``run_differs`` to pass a ``jira_key``-bearing local_state. That option was NOT
    taken: the caller is unchanged, so the suppression is exactly as correct as it was
    and removing it would un-suppress two unsound arms (``field_drift`` /
    ``unbound_local``) that duplicate work ``outbound_differ`` and ``binding_walk``
    already do.
    """
    helpers = _load_module("reconcile_helpers_3b5f", _REC / "reconcile_helpers.py")
    assert callable(helpers.drop_snapshot_differ_local_state_emissions)
    run_differs_src = (_REC / "run_differs.py").read_text(encoding="utf-8")
    assert "drop_snapshot_differ_local_state_emissions(mutations)" in run_differs_src, (
        "run_differs must still consult the snapshot-differ suppression."
    )
