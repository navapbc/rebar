"""Parametrized matrix tests for conflict_resolver per-element provenance (story 26de, task 626e).

Covers dd-1: 4 scalars (title, description, priority, assignee) and 4 collections
(comments, labels excluding dso-id, watchers, links), bidirectional, apply-once
and zero-mutations across 3 consecutive follow-on passes. Plus counter-edit
disambiguation per AC.

Per AC comment: fresh ProvenanceLedger per pass, inline dict-merge state advance
(no applier import — applier-roundtrip-divergence is out of scope here).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
DIFFER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "differ.py"
RESOLVER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "conflict_resolver.py"
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def differ():
    return _load(DIFFER_PATH, "differ_matrix_tests")


@pytest.fixture(scope="module")
def resolver():
    return _load(RESOLVER_PATH, "conflict_resolver_matrix_tests")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

JIRA_KEY = "PROJ-1"
LOCAL_ID = "id-1"


def _make_snapshot(field: str, value):
    """Build a single-issue snapshot dict shaped for differ.compute_mutations."""
    return {
        JIRA_KEY: {
            "dso_local_id": LOCAL_ID,
            field: value,
        }
    }


def _apply_mutation_to_snapshot(snapshot: dict, mutation) -> dict:
    """Inline dict-merge state advance.

    Per AC comment: NO applier.apply() invocation. Manually merge the mutation's
    ``payload`` into the snapshot's per-key dict for the given target. Returns a
    new dict (does not mutate input). Accepts a Mutation dataclass instance.
    """
    result = {k: dict(v) for k, v in snapshot.items()}
    target = mutation.target
    action = mutation.action.value
    payload = dict(mutation.payload or {})
    if action == "delete":
        result.pop(target, None)
        return result
    if action in ("create", "update"):
        if target not in result:
            result[target] = {"dso_local_id": payload.get("local_id", target)}
        result[target].update(payload)
        return result
    # probe / conflict / clean_label / repair_property: no snapshot change.
    return result


# ---------------------------------------------------------------------------
# Parametrized matrix
# ---------------------------------------------------------------------------

# 4 scalars × {create, edit}
SCALAR_CASES = [
    ("title", "Old Title", "New Title"),
    ("description", "old body", "new body"),
    ("priority", "Low", "High"),
    ("assignee", "alice", "bob"),
]

# 4 collections (labels excludes dso-id per dd-1; we test with non-dso-id labels)
COLLECTION_CASES = [
    ("comments", ["comment-a"], ["comment-a", "comment-b"]),
    ("labels", ["feature"], ["feature", "bug"]),
    ("watchers", ["alice"], ["alice", "bob"]),
    ("links", ["LINK-1"], ["LINK-1", "LINK-2"]),
]


@pytest.mark.parametrize(
    "field,old,new",
    SCALAR_CASES + COLLECTION_CASES,
    ids=[c[0] for c in SCALAR_CASES] + [c[0] for c in COLLECTION_CASES],
)
@pytest.mark.parametrize("origin", ["local", "jira"], ids=["origin-local", "origin-jira"])
def test_bidirectional_apply_once_then_idempotent(differ, field, old, new, origin):
    """For each (class, origin): pass 1 emits >=1 mutation; passes 2-4 emit zero.

    State advance between passes uses _apply_mutation_to_snapshot (inline merge).
    Fresh ProvenanceLedger per pass (resolver currently uses provenance_record=None;
    the per-pass-fresh contract is what dd-1 asserts).
    """
    # Pass 1: divergent state. ``prev`` is the remote/jira snapshot, ``next_`` is
    # local. Direction of the edit depends on origin.
    if origin == "local":
        prev = _make_snapshot(field, old)
        next_ = _make_snapshot(field, new)
    else:
        prev = _make_snapshot(field, new)
        next_ = _make_snapshot(field, old)

    pass1 = differ.compute_mutations(prev, next_)
    assert len(pass1) >= 1, (
        f"field={field} origin={origin}: pass 1 emitted no mutations "
        "— expected apply-once mutation"
    )

    # Advance BOTH sides by applying the emitted mutation (post-write convergence).
    prev_after = _apply_mutation_to_snapshot(prev, pass1[0])
    next_after = _apply_mutation_to_snapshot(next_, pass1[0])

    # Passes 2, 3, 4: fresh state, fresh implicit ledger. Expect zero mutations.
    for pass_n in (2, 3, 4):
        muts = differ.compute_mutations(prev_after, next_after)
        update_muts = [m for m in muts if m.action.value == "update"]
        assert update_muts == [], (
            f"field={field} origin={origin} pass={pass_n}: "
            f"expected zero update mutations after apply-once, got {update_muts}"
        )


def test_labels_matrix_excludes_dso_id(differ):
    """Per dd-1: labels matrix must exclude dso-id-* labels.

    A change to a dso-id-* label MUST NOT emit a mutation, even when other labels
    differ. (dso-id is an identity marker, not a user-facing label.)
    """
    prev = {
        JIRA_KEY: {
            "dso_local_id": LOCAL_ID,
            "dso-id": "dso-id:OLD",
            "labels": ["feature"],
        }
    }
    next_ = {
        JIRA_KEY: {
            "dso_local_id": LOCAL_ID,
            "dso-id": "dso-id:NEW",
            "labels": ["feature"],
        }
    }
    muts = differ.compute_mutations(prev, next_)
    # Only dso-id changed — and dso-id is in EXCLUDED_FIELDS — so zero mutations.
    assert muts == [], (
        f"dso-id change should be excluded from labels matrix; got mutations: {muts}"
    )


def test_provenance_ledger_records_side_and_timestamp(resolver):
    """Per-element provenance must record side-of-origin AND timestamp (sc-11/sc-12).

    Asserts the resolver exposes a ``ProvenanceLedger`` class (or equivalent) and
    that recording an element yields a record with both ``side`` and ``timestamp``
    fields. This is the RED signal for the implementer to add per-element
    provenance — current conflict_resolver.py only has resolve_set_valued's
    in-place provenance_record list with no side/timestamp metadata.
    """
    assert hasattr(resolver, "ProvenanceLedger"), (
        "conflict_resolver must export ProvenanceLedger for per-element provenance "
        "(story 26de dd: 'Per-element provenance records both side-of-origin and timestamp')"
    )
    ledger = resolver.ProvenanceLedger()
    ledger.record(
        element_key="labels:feature",
        side="local",
        value="feature",
    )
    serialized = ledger.serialize()
    assert serialized, "ProvenanceLedger.serialize() must yield non-empty output"
    # Pick the first record (shape may be list-of-dicts or dict-of-dicts).
    if isinstance(serialized, list):
        record = serialized[0]
    elif isinstance(serialized, dict):
        record = next(iter(serialized.values()))
    else:
        pytest.fail(f"unexpected serialize() shape: {type(serialized).__name__}")
    assert "side" in record, f"provenance record missing 'side' field: {record}"
    assert "timestamp" in record, f"provenance record missing 'timestamp' field: {record}"


def test_echo_suppression_uses_provenance(differ, resolver):
    """Echo suppression: a write recorded as local-origin on pass N must not
    re-emit as an outbound mutation on pass N+1 even when the jira-side snapshot
    has not yet caught up.

    Failure mode without provenance: differ sees jira (old) vs local (new) and
    emits another update — duplicating the prior write.
    """
    assert hasattr(resolver, "ProvenanceLedger"), (
        "ProvenanceLedger required for echo-suppression (story 26de echo test)"
    )
    # Pass N: emit a mutation and record provenance.
    prev = _make_snapshot("labels", ["feature"])
    next_ = _make_snapshot("labels", ["feature", "bug"])
    mutations = differ.compute_mutations(prev, next_)
    assert mutations, "expected a mutation on pass N"

    ledger = resolver.ProvenanceLedger()
    ledger.record(element_key="labels:bug", side="local", value="bug")

    # Pass N+1: jira snapshot has NOT yet caught up (still has ['feature']).
    # Local still has ['feature', 'bug']. Without provenance, differ would
    # emit another update. With provenance + ledger, the differ must
    # accept the ledger and suppress the echo.
    assert hasattr(differ, "compute_mutations_with_ledger"), (
        "differ must accept a ledger to consult for echo suppression "
        "(story 26de echo-suppression test)"
    )
    muts = differ.compute_mutations_with_ledger(prev, next_, ledger=ledger)
    update_muts = [m for m in muts if m.action.value == "update"]
    assert update_muts == [], (
        f"echo not suppressed: prior local-origin write re-emitted as {update_muts}"
    )


@pytest.mark.parametrize(
    "field,old,new",
    SCALAR_CASES,
    ids=[c[0] for c in SCALAR_CASES],
)
def test_counter_edit_not_suppressed(differ, field, old, new):
    """Counter-edit disambiguation per AC.

    Pass 1: local→jira write of (old→new). Apply to both sides.
    Pass 2 setup: simulate operator edit on jira side to a DIFFERENT value.
    Assert pass 2 emits a mutation (NOT suppressed by an echo gate that
    treats any post-pass-1 divergence as an echo).
    """
    # Pass 1: local has new, jira has old.
    prev = _make_snapshot(field, old)
    next_ = _make_snapshot(field, new)
    pass1 = differ.compute_mutations(prev, next_)
    assert pass1, f"counter-edit setup ({field}): pass 1 should emit a mutation"

    # Apply pass-1 mutation to both sides.
    prev_after = _apply_mutation_to_snapshot(prev, pass1[0])
    next_after = _apply_mutation_to_snapshot(next_, pass1[0])

    # Operator counter-edit on jira side: change to a third, distinct value.
    counter_value = f"COUNTER-{new}"
    prev_after[JIRA_KEY][field] = counter_value

    # Pass 2: jira has counter_value, local still has new — divergence is REAL,
    # not an echo. Expect a non-empty mutation list for this field.
    pass2 = differ.compute_mutations(prev_after, next_after)
    field_changes = [
        m for m in pass2
        if m.action.value == "update" and field in dict(m.payload or {})
    ]
    assert field_changes, (
        f"counter-edit on {field}: pass 2 should emit a mutation reflecting "
        f"the jira-side counter-edit (got: {pass2})"
    )
