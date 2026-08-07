"""Inbound direction-preservation cells for the LIVE canonical outbound field diff.

ADR 0026 names five inbound-mirrored scalar fields — ``title``, ``description``,
``priority``, ``status``, ``assignee`` — and mandates one arbitration rule for each:
when local equals the baseline (no local edit since the last sync), a differing remote
value must flow INBOUND, so the outbound diff has to suppress its local-wins push.

The gap this file closes: only ``title`` was ever pinned against the module the
production pass actually executes (``rebar_reconciler.outbound_field_diff``). The
other four fields are pinned only in
``tests/unit/rebar_reconciler/diffing/test_inbound_field_sync_directionality.py``
against ``rebar_reconciler.adapters.jira.outbound_fields._diff_fields`` — a function
that, since ticket 625b re-homed the diff into the vendor-neutral core, has NO
production caller. Those cells therefore pin a dead code path: they can stay green
while the live path regresses on ``description``/``priority``/``status``/``assignee``.

Everything below drives ``outbound_field_diff.diff_canonical_fields`` (the function
``compute_update_fields`` invokes, which is what ``outbound_differ`` imports and calls),
in all three arbitration states per field: suppression, genuine local edit, and the
no-ancestor degrade to local-wins.
"""

from __future__ import annotations

from typing import Any

import pytest

from rebar_reconciler.outbound_field_diff import compute_update_fields, diff_canonical_fields

# --- the live-path harness (same shape as the existing ``title`` cells) ----------


def _canonical(**ov: Any) -> dict[str, Any]:
    """A canonical (LOCAL-shaped) field dict, the shape the InboundMapper emits."""
    f: dict[str, Any] = {
        "title": "T",
        "description": "D",
        "priority": 2,
        "status": "open",
        "assignee": "",
    }
    f.update(ov)
    return f


class _PassthroughOutboundMapper:
    """The OutboundMapper port, reduced to the two operations the diff calls."""

    def map_fields_to_remote(self, changed: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        return dict(changed)

    def resolve_assignee(
        self,
        local_value: Any,
        _remote_identity: Any,
        *,
        assignee_resolver: Any = None,
    ) -> tuple[Any, bool, bool]:
        return (local_value, False, False)


def _live_diff(
    ticket: dict[str, Any], remote: dict[str, Any], baseline: dict[str, Any] | None
) -> dict[str, Any]:
    """Run the LIVE canonical diff — the exact function the production pass calls."""
    return diff_canonical_fields(
        ticket,
        remote,
        baseline,
        outbound_mapper=_PassthroughOutboundMapper(),
        jira_key="DIG-1",
        local_id="loc-1",
    )


def _ticket(**ov: Any) -> dict[str, Any]:
    """A local ticket in canonical shape, with the two non-diffed identity keys."""
    return {"ticket_id": "loc-1", "ticket_type": "task", **_canonical(**ov)}


def _visible(changed: dict[str, Any]) -> dict[str, Any]:
    """``changed`` minus the private sentinels, for readable assertion messages."""
    return {k: v for k, v in changed.items() if not k.startswith("_")}


# --- production-reachability guard ----------------------------------------------
#
# This test exists so the cells below cannot degrade into the 599e / 24f7
# "passing by not running" failure class — the exact class the adapter-level
# ``_diff_fields`` cells fell into when ticket 625b moved the live diff elsewhere.
# It pins the two links in the chain from the production caller to the function
# these cells exercise, so if either is re-homed again this file fails loudly
# instead of quietly pinning dead code.


def test_the_function_under_test_is_the_one_production_calls() -> None:
    """Pin that ``diff_canonical_fields`` is reachable from the production caller.

    Two links, both asserted against the real modules rather than restated:
    (1) ``outbound_differ`` — the pass's UPDATE path — binds the very
    ``compute_update_fields`` object this module exposes; and (2) the
    ``diff_canonical_fields`` name resolved inside ``compute_update_fields``' own
    module globals (i.e. the callee it will actually invoke at runtime) is the
    object every cell below drives. Without this, a future re-home would leave the
    cells green while pinning a function nothing calls.
    """
    from rebar_reconciler import outbound_differ

    assert outbound_differ.compute_update_fields is compute_update_fields, (
        "outbound_differ must call THIS compute_update_fields; if this binding moves, "
        "the cells below stop covering the production path"
    )
    invoked = compute_update_fields.__globals__["diff_canonical_fields"]
    assert invoked is diff_canonical_fields, (
        "compute_update_fields resolves 'diff_canonical_fields' from its module globals; "
        "the cells below must drive that same object"
    )


# --- per-field case data --------------------------------------------------------
#
# (field, baseline value, local value, remote value). Shapes are per-field:
# ``title``/``description``/``assignee`` are strings, ``priority`` is an int (the
# canonical local priority; default 2), ``status`` uses LOCAL names because
# ``diff_canonical_fields`` takes dicts already canonicalized by the InboundMapper.

_SUPPRESSION_CASES = [
    ("title", "OLD", "OLD", "NEW from Jira"),
    ("description", "old body", "old body", "new body from Jira"),
    ("priority", 2, 2, 1),
    ("status", "open", "open", "in_progress"),
    ("assignee", "alice@x.com", "alice@x.com", "bob@x.com"),
]

_LOCAL_EDIT_CASES = [
    ("title", "OLD", "locally edited", "OLD"),
    ("description", "old body", "locally edited", "old body"),
    ("priority", 2, 1, 2),
    ("status", "open", "closed", "open"),
    ("assignee", "alice@x.com", "bob@x.com", "alice@x.com"),
]

_NO_BASELINE_CASES = [
    ("title", "LOCAL", "NEW from Jira"),
    ("description", "LOCAL body", "new body from Jira"),
    ("priority", 1, 3),
    ("status", "closed", "open"),
    ("assignee", "carol@x.com", "bob@x.com"),
]


def _ids(cases: list[tuple[Any, ...]]) -> list[str]:
    return [str(c[0]) for c in cases]


# --- 1. suppression: local == baseline, remote moved -> mirror inbound -----------


@pytest.mark.parametrize(
    ("field", "base_val", "local_val", "remote_val"),
    _SUPPRESSION_CASES,
    ids=_ids(_SUPPRESSION_CASES),
)
def test_remote_edit_is_suppressed_when_local_matches_baseline(
    field: str, base_val: Any, local_val: Any, remote_val: Any
) -> None:
    """Pin ADR 0026's core arbitration on the LIVE path for every mirrored field: a
    remote-side edit, with local unchanged since the baseline, must NOT be emitted.

    Why it matters: emitting the field makes the inbound differ drop its own update as
    a same-pass contradiction (inbound bidirectional suppression, bug 3bf8), so the
    teammate's Jira-side edit never reaches the local ticket AND the stale local value
    is pushed back over it — a silent data loss that the pass reports as convergence.
    Four of these five fields were previously pinned only against the caller-less
    adapter helper, so the live path could regress undetected.
    """
    baseline = _canonical(**{field: base_val})
    ticket = _ticket(**{field: local_val})
    remote = _canonical(**{field: remote_val})

    changed = _live_diff(ticket, remote, baseline)

    assert field not in changed, (
        f"a remote-side {field} edit must mirror inbound, not be reverted by "
        f"local-wins; changed={_visible(changed)}"
    )


# --- 2. the other half: local != baseline -> local-wins still pushes -------------


@pytest.mark.parametrize(
    ("field", "base_val", "local_val", "remote_val"),
    _LOCAL_EDIT_CASES,
    ids=_ids(_LOCAL_EDIT_CASES),
)
def test_genuine_local_edit_still_emits_outbound(
    field: str, base_val: Any, local_val: Any, remote_val: Any
) -> None:
    """Pin the complement of the suppression rule for every mirrored field: local !=
    baseline is a genuine local edit, so local-wins must still push it outbound.

    Why it matters: a suppression fix that over-fires is the mirror-image bug — every
    local edit would be silently discarded and the reconciler would never converge.
    These cells bound the suppression so it can only ever fire on an UNCHANGED local
    value. The assignee case additionally exercises the resolver seam: the passthrough
    ``resolve_assignee`` returns ``(local_value, False, False)``, i.e. a
    non-authoritative value that must still be emitted verbatim.
    """
    baseline = _canonical(**{field: base_val})
    ticket = _ticket(**{field: local_val})
    remote = _canonical(**{field: remote_val})

    changed = _live_diff(ticket, remote, baseline)

    assert changed.get(field) == local_val, (
        f"a genuine local {field} edit must still push outbound; changed={_visible(changed)}"
    )


# --- 3. no ancestor -> no arbitration possible -> unchanged local-wins -----------


@pytest.mark.parametrize(
    ("field", "local_val", "remote_val"), _NO_BASELINE_CASES, ids=_ids(_NO_BASELINE_CASES)
)
def test_absent_baseline_degrades_to_local_wins(
    field: str, local_val: Any, remote_val: Any
) -> None:
    """Pin the partial-tolerance degrade for every mirrored field: with no baseline at
    all, and with a baseline that merely OMITS the field, the diff falls back to
    pre-arbitration local-wins and emits the local value.

    Why it matters: the baseline populates lazily (a confirmed binding can be one pass
    cold — see ``emit_baseline_cold_start``), so ``None`` and partial baselines are
    ordinary runtime states, not edge cases. If ``_suppressed_by_inbound`` treated a
    missing entry as "unchanged", every cold or partially-populated binding would stop
    pushing local edits entirely. Both shapes are asserted because they reach the guard
    through different conditions (``canonical_baseline or {}`` vs ``field not in
    baseline``).
    """
    ticket = _ticket(**{field: local_val})
    remote = _canonical(**{field: remote_val})

    assert _live_diff(ticket, remote, None).get(field) == local_val, (
        f"with no baseline, {field} keeps unchanged local-wins behaviour"
    )

    partial = {k: v for k, v in _canonical().items() if k != field}
    assert _live_diff(ticket, remote, partial).get(field) == local_val, (
        f"a baseline that omits {field} is the same 'no ancestor' case"
    )
