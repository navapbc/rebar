"""Bug e6e9 — the baseline must record what rebar SYNCED, not what it last FETCHED.

The defect: ``reconcile._advance_baselines`` advanced the ADR-0026 baseline from the
pass-START snapshot, which is fetched BEFORE the outbound apply. For any field rebar
itself pushed in that pass, the baseline was left holding Jira's PRE-push value. ADR 0026
makes ``local == baseline`` the sole direction signal, read as "local did not change, so a
differing remote is a Jira-side edit — suppress outbound and let inbound mirror it". A
REVERT to the pre-push value lands exactly in that window: local equals the stale baseline,
outbound stands down, and the inbound differ (which has no arbitration of its own and can
only clobber a field outbound left silent) mirrors Jira's now-stale value back over the
local revert.

Observed in production on ``858c-786a-13be-4332`` (open -> closed) and
``556a-5a1f-adb3-4139`` (open -> in_progress), and 18 days earlier on 8 tickets at once.

Three properties are pinned here, and the third is what makes the fix safe rather than
dangerous:

1. **ABA** — push A, revert to B before the next pass, and the revert SURVIVES.
2. **Held firm** — a genuine Jira-side edit against a truly unchanged local value still
   flows inbound. Without this the fix could silently degrade into the rejected option B
   (never let inbound win) and nobody would notice.
3. **Soft-fail** — a push that did NOT land must NOT advance the baseline. A transition can
   soft-fail while the pass still exits 0, and a falsely-advanced baseline does not
   self-correct, whereas today's clobber does. Lagging is recoverable; leading is not.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_ENGINE = Path(__file__).resolve().parents[4] / "src" / "rebar" / "_engine"
if str(_ENGINE) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(_ENGINE))

from rebar_reconciler.binding_store import BindingStore  # noqa: E402
from rebar_reconciler.inbound_fields import _map_jira_to_local_fields  # noqa: E402
from rebar_reconciler.outbound_field_diff import diff_canonical_fields  # noqa: E402
from rebar_reconciler.reconcile import _advance_baselines  # noqa: E402

# The five inbound-mirrored fields share one guard (``_suppressed_by_inbound``) and
# therefore one ABA exposure, so all five are covered — not just the status field that
# happened to break in production.
#
# Each row is: (case id, vendor field, the vendor value rebar PUSHES, the pre-push vendor
# value the operator reverts to, the local field name, the local value that reverts).
_FIELDS: list[tuple[str, str, Any, Any, str, Any]] = [
    ("status", "status", "Done", "To Do", "status", "open"),
    ("summary", "summary", "pushed title", "OLD title", "title", "OLD title"),
    ("description", "description", "pushed body", "OLD body", "description", "OLD body"),
    ("priority", "priority", "High", "Medium", "priority", 2),
    ("assignee", "assignee", "bob@x.com", "alice@x.com", "assignee", "alice@x.com"),
]


def _vendor_snapshot_entry(**ov: Any) -> dict[str, Any]:
    """A Jira snapshot entry, in the raw vendor shape the fetcher produces."""
    entry: dict[str, Any] = {
        "summary": "OLD title",
        "description": "OLD body",
        "priority": {"name": "Medium"},
        "status": {"name": "To Do"},
        "assignee": "alice@x.com",
    }
    entry.update(ov)
    return entry


def _local_ticket(**ov: Any) -> dict[str, Any]:
    """The local ticket, canonical (local-shaped)."""
    t: dict[str, Any] = {
        "ticket_id": "loc-1",
        "ticket_type": "task",
        "title": "OLD title",
        "description": "OLD body",
        "priority": 2,
        "status": "open",
        "assignee": "alice@x.com",
    }
    t.update(ov)
    return t


class _PassthroughOutboundMapper:
    """The OutboundMapper port, reduced to the two operations the diff calls."""

    def map_fields_to_remote(self, changed: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        return dict(changed)

    def resolve_assignee(
        self, local_value: Any, _remote_identity: Any, *, assignee_resolver: Any = None
    ) -> tuple[Any, bool, bool]:
        return (local_value, False, False)


def _store(tmp_path: Path) -> BindingStore:
    s = BindingStore(tmp_path / ".tickets-tracker")
    s.bind_confirm("loc-1", "REB-1")
    return s


def _next_pass_outbound(
    store: BindingStore, ticket: dict[str, Any], remote_entry: dict[str, Any]
) -> dict[str, Any]:
    """Run the NEXT pass's outbound arbitration exactly as production does.

    Reads the baseline back off the store, canonicalizes it through the same inbound
    mapper ``compute_update_fields`` uses, and drives ``diff_canonical_fields`` — the
    function the production ``outbound_differ`` -> ``compute_update_fields`` chain calls.
    Whether a field appears in the result IS the arbitration outcome: the inbound differ
    clobbers a field only in a pass where outbound emitted nothing for it.
    """
    baseline = store.get_baseline("loc-1")
    return diff_canonical_fields(
        ticket,
        _map_jira_to_local_fields(remote_entry),
        _map_jira_to_local_fields(baseline) if baseline is not None else None,
        outbound_mapper=_PassthroughOutboundMapper(),
        jira_key="REB-1",
        local_id="loc-1",
    )


# --- 1. the ABA oracle ----------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "vendor_field", "pushed", "reverted_vendor", "local_field", "reverted_local"),
    _FIELDS,
    ids=[row[0] for row in _FIELDS],
)
def test_local_revert_survives_the_pass_after_our_own_push(
    tmp_path: Path,
    case: str,
    vendor_field: str,
    pushed: Any,
    reverted_vendor: Any,
    local_field: str,
    reverted_local: Any,
) -> None:
    """A -> B -> A: the operator's revert to the pre-push value must NOT be clobbered.

    Sequence, matching the production incident exactly:
      * Pass N fetches Jira (still holding B) BEFORE applying, then rebar pushes A.
      * The operator reverts local back to B inside that window.
      * Pass N+1 must still emit the field outbound, because local (B) genuinely differs
        from what was last SYNCED (A). If it does not, the inbound differ sees a field
        outbound left silent and mirrors Jira's A back over the revert.

    Before the fix the baseline held B (the pass-start fetch), so local B == baseline B,
    the guard fired, and the field was suppressed — the clobber.
    """
    store = _store(tmp_path)
    pass_start_fetch = {"REB-1": _vendor_snapshot_entry()}

    # Pass N: fetch (pre-push) advances the baseline; our own confirmed push overlays it.
    _advance_baselines(store, pass_start_fetch, {"loc-1": {vendor_field: pushed}})

    assert store.get_baseline("loc-1")[vendor_field] == pushed, (
        f"the baseline must record the {vendor_field} rebar actually SYNCED ({pushed!r}), "
        f"not the pre-push fetch ({reverted_vendor!r}) — ADR 0026 defines it as the "
        f"last-synced value"
    )

    # Pass N+1: Jira holds A; the operator has reverted local to B.
    changed = _next_pass_outbound(
        store,
        _local_ticket(**{local_field: reverted_local}),
        _vendor_snapshot_entry(**{vendor_field: pushed}),
    )

    assert changed.get(local_field) == reverted_local, (
        f"the local revert of {local_field} to {reverted_local!r} must be re-pushed, not "
        f"suppressed; outbound emitting nothing here is exactly what lets the inbound "
        f"differ mirror Jira's {pushed!r} back over it. changed="
        f"{ {k: v for k, v in changed.items() if not k.startswith('_')} }"
    )


# --- 2. held firm: the ADR 0026 behaviour must NOT be weakened -------------------


@pytest.mark.parametrize(
    ("case", "vendor_field", "pushed", "reverted_vendor", "local_field", "reverted_local"),
    _FIELDS,
    ids=[row[0] for row in _FIELDS],
)
def test_a_genuine_remote_edit_still_flows_inbound(
    tmp_path: Path,
    case: str,
    vendor_field: str,
    pushed: Any,
    reverted_vendor: Any,
    local_field: str,
    reverted_local: Any,
) -> None:
    """The complement, and the guard against the fix quietly becoming option B.

    Local has NOT changed since the (now correctly advanced) baseline, and Jira has. That
    is a genuine remote-side edit, which ADR 0026 Decision 1 deliberately chose to mirror
    inbound. Outbound must stand down. A "fix" that simply stopped suppressing would pass
    the ABA oracle above while silently discarding every Jira-side edit — the operator
    rejected exactly that trade, so it is pinned here for all five fields.
    """
    store = _store(tmp_path)
    _advance_baselines(store, {"REB-1": _vendor_snapshot_entry()}, {"loc-1": {}})

    # Local still holds the baseline value; Jira moved underneath us.
    changed = _next_pass_outbound(
        store,
        _local_ticket(),
        _vendor_snapshot_entry(**{vendor_field: pushed}),
    )

    assert local_field not in changed, (
        f"a genuine Jira-side {local_field} edit must mirror inbound, not be reverted by "
        f"an outbound local-wins push; changed="
        f"{ {k: v for k, v in changed.items() if not k.startswith('_')} }"
    )


# --- 3. soft-fail: a push that did not land must not advance the baseline --------


def test_a_soft_failed_push_does_not_advance_the_baseline(tmp_path: Path) -> None:
    """The constraint that makes this fix safe rather than dangerous.

    A status transition can soft-fail while the pass still exits 0 — an unreachable Jira
    transition raises out of ``transition_issue_by_name`` into the applier's per-mutation
    backstop, and a 400 illegal-transition is answered with a comment instead of the edit.
    Neither landed, so neither may appear in the synced map, and the baseline must keep the
    fetched value.

    Why this matters more than the bug being fixed: today's clobber is SELF-LIMITING (the
    baseline advances to Jira's value, local then differs, and the next pass pushes local
    back out). A falsely-advanced baseline instead makes rebar believe local and Jira agree
    when they do not, and it never self-corrects. Advancing on "the pass ran" would trade a
    recoverable bug for silent divergence.
    """
    store = _store(tmp_path)

    # The mutation was EMITTED but its write soft-failed, so it contributes nothing.
    _advance_baselines(store, {"REB-1": _vendor_snapshot_entry()}, {"loc-1": {}})

    assert store.get_baseline("loc-1")["status"] == "To Do", (
        "a soft-failed transition must leave the baseline at the FETCHED value; recording "
        "a sync that never happened would mask real divergence and would not self-correct"
    )

    # And the arbitration consequence: local is unchanged, so the suppression still holds
    # exactly as it did before — the failure changes nothing about direction.
    changed = _next_pass_outbound(store, _local_ticket(), _vendor_snapshot_entry())
    assert "status" not in changed


def test_only_the_confirmed_fields_of_a_partly_applied_mutation_advance(
    tmp_path: Path,
) -> None:
    """A mutation carrying several fields advances ONLY those reported as landed.

    ``update_issue`` fans out to separate REST sub-calls (priority, then the transition,
    then the field edit), so a late failure can leave an early field landed. The dispatch
    layer reports all-or-nothing per call and therefore UNDER-reports here; this pins that
    an under-report is harmless — the unreported field simply keeps the fetched baseline
    and gets re-pushed next pass — while the reported field advances.
    """
    store = _store(tmp_path)
    _advance_baselines(
        store,
        {"REB-1": _vendor_snapshot_entry()},
        {"loc-1": {"summary": "pushed title"}},
    )

    baseline = store.get_baseline("loc-1")
    assert baseline["summary"] == "pushed title", "the confirmed field advances"
    assert baseline["status"] == "To Do", "an unreported field keeps the fetched value"
    assert baseline["description"] == "OLD body"


# --- 4. the advance's own edges -------------------------------------------------


def test_an_empty_synced_map_is_byte_identical_to_the_pre_fix_advance(
    tmp_path: Path,
) -> None:
    """A pass with no outbound writes must behave exactly as it did before this change.

    The overwhelming majority of passes push nothing. Pinning equivalence with the
    fetch-only advance keeps this fix from being a behaviour change for them.
    """
    fetch = {"REB-1": _vendor_snapshot_entry()}

    with_empty = _store(tmp_path / "a")
    _advance_baselines(with_empty, fetch, {})
    without = _store(tmp_path / "b")
    _advance_baselines(without, fetch)

    assert with_empty.get_baseline("loc-1") == without.get_baseline("loc-1")
    assert without.get_baseline("loc-1")["status"] == "To Do"


def test_an_out_of_window_binding_still_receives_its_overlay(tmp_path: Path) -> None:
    """Our own confirmed write is evidence even when the fetch did not cover the key.

    ``_advance_baselines`` skips a binding whose Jira key is outside the fetch window,
    because the fetch has nothing to say about it. A write WE issued is different: it
    cannot be missing. Skipping the overlay here would leave precisely the stale baseline
    this bug is about, on the tickets least likely to be re-fetched soon.
    """
    store = _store(tmp_path)
    _advance_baselines(store, {"REB-1": _vendor_snapshot_entry()})  # seed a baseline
    _advance_baselines(store, {}, {"loc-1": {"status": "Done"}})  # key out of window

    baseline = store.get_baseline("loc-1")
    assert baseline["status"] == "Done", "the confirmed push overlays even out of window"
    assert baseline["summary"] == "OLD title", (
        "and it is a per-field MERGE — an untouched field keeps its last fetched value "
        "rather than being dropped by a whole-dict replace"
    )


def test_an_unknown_or_unconfirmed_local_id_in_the_synced_map_is_a_no_op(
    tmp_path: Path,
) -> None:
    """A synced entry for a local id the store does not carry must not create a binding.

    The synced map is keyed by whatever the applier saw on the mutation; a stale or
    unbound id must be ignored rather than materializing a baseline for a pair that does
    not exist.
    """
    store = _store(tmp_path)
    _advance_baselines(
        store, {"REB-1": _vendor_snapshot_entry()}, {"loc-nonexistent": {"status": "Done"}}
    )

    assert store.get_baseline("loc-1")["status"] == "To Do"
    assert store.get_baseline("loc-nonexistent") is None


def test_advance_survives_a_store_without_merge_baseline(tmp_path: Path) -> None:
    """A store predating ``merge_baseline`` degrades to the fetch-only advance, not a raise.

    Same getattr-guarded contract ``_advance_peer_parent`` already uses for
    ``set_peer_parent``: older stores and test doubles must fall through to the previous
    behaviour rather than taking down a sync pass mid-flight.
    """

    class _LegacyStore:
        def __init__(self) -> None:
            self.baselines: dict[str, dict] = {}

        def all_bindings(self) -> dict[str, dict]:
            return {"loc-1": {"state": "confirmed", "jira_key": "REB-1"}}

        def set_baseline(self, local_id: str, fields: dict) -> None:
            self.baselines[local_id] = dict(fields)

    legacy = _LegacyStore()
    assert _advance_baselines(legacy, {"REB-1": _vendor_snapshot_entry()}, {"loc-1": {}}) == 1
    assert legacy.baselines["loc-1"]["status"] == {"name": "To Do"}
