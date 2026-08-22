"""RP-06 S7 — the public review-output narrowness contract (AC4).

Proves that the cross-gate cutover kept the PUBLIC response schemas NARROW — ``review-plan``
(``plan_review_verdict``), ``review-plan --status`` (``plan_review_status``), and the
code-review outputs (``code_review_verdict`` / ``review_result``) — while the reducer-IGNORED
internal journal (the kernel's ``unit_trace`` record) retains a VERSIONED internal trace.

The public surface must never grow per-unit discovery/trace/debug fields; those live only in
the journal, stamped with ``DISCOVERY_NAMESPACE_VERSION`` so the internal trace can evolve
without touching the frozen public shape. Assertions are on observable contracts: the schemas'
declared top-level property sets and the shape of the real ``unit_trace`` record.
"""

from __future__ import annotations

import pytest

from rebar import schemas
from rebar.llm.review_kernel import discovery
from rebar.llm.review_kernel.discovery import (
    DiscoveryUnitPlan,
    UnitOutcome,
    Usage,
    unit_trace,
)

pytestmark = pytest.mark.unit


# The pinned, NARROW top-level property sets of each public review output. Growing any of
# these (especially with a per-unit trace) is a deliberate, reviewable change to this pin.
_NARROW_PROPERTIES = {
    schemas.PLAN_REVIEW_VERDICT: {
        "verdict",
        "ticket_id",
        "ticket_type",
        "blocking",
        "advisory",
        "coaching",
        "indeterminate",
        "overflow",
        "dropped",
        "coverage",
        "material_fingerprint",
        "signature",
        "sidecar_emitted",
        "runner",
        "model",
    },
    schemas.PLAN_REVIEW_STATUS: {"ok", "verdict", "reason", "verified_at_sha", "signed_at"},
    schemas.CODE_REVIEW_VERDICT: {
        "verdict",
        "blocking",
        "advisory",
        "coaching",
        "coverage",
        "runner",
        "model",
    },
    schemas.REVIEW_RESULT: {
        "findings",
        "target",
        "reviewers",
        "runner",
        "model",
        "trace_id",
        "summary",
    },
}

# Any of these declared as a TOP-LEVEL public property would be a per-unit trace leak.
_FORBIDDEN_TRACE_KEYS = frozenset(
    {
        "discovery_trace",
        "discovery_traces",
        "unit_trace",
        "unit_traces",
        "traces",
        "per_unit",
        "per_unit_trace",
        "lineage",
        "envelope",
        "checkpoint",
        "checkpoints",
        "debug",
    }
)


@pytest.mark.parametrize("name", sorted(_NARROW_PROPERTIES))
def test_public_output_schema_keeps_its_narrow_property_set(name: str) -> None:
    """The declared top-level property set is EXACTLY the pinned narrow set — no per-unit
    trace/debug field crept in."""
    declared = set(schemas.load(name).get("properties", {}))
    assert declared == _NARROW_PROPERTIES[name], (
        f"{name!r} top-level properties drifted from the narrow contract: "
        f"added={sorted(declared - _NARROW_PROPERTIES[name])} "
        f"removed={sorted(_NARROW_PROPERTIES[name] - declared)}"
    )


@pytest.mark.parametrize("name", sorted(_NARROW_PROPERTIES))
def test_no_public_output_schema_declares_a_trace_key(name: str) -> None:
    declared = set(schemas.load(name).get("properties", {}))
    assert not (declared & _FORBIDDEN_TRACE_KEYS), (
        f"{name!r} declares forbidden per-unit trace key(s): "
        f"{sorted(declared & _FORBIDDEN_TRACE_KEYS)}"
    )


def test_internal_journal_trace_is_versioned() -> None:
    """The reducer-ignored internal journal record (``unit_trace``) retains a VERSIONED trace
    stamped with the discovery namespace version — the private surface that is allowed to grow
    while the public verdict stays frozen."""
    unit = DiscoveryUnitPlan(
        unit_id="u1",
        prompt_id="p",
        contract_id="c",
        model="m",
        mode="single",
        context_digest="",
        policy_digest="",
        dependencies=("dep",),
    )
    outcome = UnitOutcome(unit_id="u1", kind="success", usage=Usage(1, 2, 1))
    record = unit_trace(outcome, unit_plan=unit)
    assert record["namespace_version"] == discovery.DISCOVERY_NAMESPACE_VERSION
    assert record["kind"] == "success"
    assert record["unit_id"] == "u1"


def test_the_versioned_journal_trace_keys_are_absent_from_public_schemas() -> None:
    """The internal trace's field names do not leak onto ANY public output schema — the
    journal and the verdict are disjoint surfaces."""
    unit = DiscoveryUnitPlan(
        unit_id="u1",
        prompt_id="p",
        contract_id="c",
        model="m",
        mode="single",
        context_digest="",
        policy_digest="",
    )
    outcome = UnitOutcome(unit_id="u1", kind="skipped", usage=Usage())
    trace_keys = set(unit_trace(outcome, unit_plan=unit))
    # The journal-only keys that must never be public verdict properties.
    journal_only = trace_keys & {"lineage", "namespace_version"}
    assert journal_only, "the internal trace is expected to carry versioned lineage keys"
    for name in _NARROW_PROPERTIES:
        declared = set(schemas.load(name).get("properties", {}))
        assert not (declared & journal_only), (
            f"{name!r} leaks internal journal key(s) {sorted(declared & journal_only)}"
        )
