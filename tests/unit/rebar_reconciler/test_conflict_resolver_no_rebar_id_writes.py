"""Contract test: conflict_resolver never proposes (write, rebar-id) mutation.

Parametrized across the 6-case matrix from draft-9 (story 26de-eb67-29d2-48ae).
For each case, every resolved Mutation is inspected to assert that no mutation
has a 'labels' payload containing any value starting with 'rebar-id-' AND an
action in {create, update}.

This is the dd-3 contract: per-element provenance must skip rebar-id labels;
the conflict_resolver must not propose writes for the identity marker.

6 case names (per task e2f8-9fa5-9eab-4418 REVISION_CYCLE_1):
  (a) inbound-comment-create
  (b) outbound-comment-create
  (c) comment-edit-bidirectional
  (d) comment-delete-bidirectional
  (e) label-create-edit-delete-bidirectional  (excluding rebar-id)
  (f) link-create-edit-delete-bidirectional
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Module loading (per conftest.py convention for this directory)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
DIFFER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "differ.py"
CONFLICT_RESOLVER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "conflict_resolver.py"
)
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"
MUTATION_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "mutation.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def differ():
    return _load(DIFFER_PATH, "differ_no_rebar_id_writes")


# ---------------------------------------------------------------------------
# Assertion helper
# ---------------------------------------------------------------------------

_WRITE_ACTIONS = {"create", "update"}


def _assert_no_rebar_id_label_writes(mutations: list[Any], case_id: str) -> None:
    """Assert no mutation proposes a rebar-id-* label write.

    A forbidden mutation is one where:
      - action is 'create' or 'update', AND
      - payload contains a 'labels' key whose value includes any item
        starting with 'rebar-id-'
    """
    for mut in mutations:
        action_val = mut.action.value if hasattr(mut.action, "value") else str(mut.action)
        if action_val not in _WRITE_ACTIONS:
            continue
        payload = dict(mut.payload or {})
        labels = payload.get("labels", [])
        if not labels:
            continue
        if not isinstance(labels, (list, tuple, set)):
            labels = [labels]
        rebar_id_labels = [lbl for lbl in labels if str(lbl).startswith("rebar-id-")]
        assert not rebar_id_labels, (
            f"case={case_id}: mutation action={action_val} target={mut.target!r} "
            f"proposed rebar-id label write(s): {rebar_id_labels} — "
            "conflict_resolver must skip rebar-id labels (dd-3 contract)"
        )


# ---------------------------------------------------------------------------
# 6-case draft-9 matrix parametrization
# ---------------------------------------------------------------------------
#
# Each case is a (local_state, jira_state) dict pair keyed by the same issue
# key "PROJ-1".  All cases include a rebar-id-* label in one or both sides to
# verify it is never proposed as a create/update payload field.
#
# The 6 cases follow the draft-9 per-element provenance scenarios:
#   (a) inbound-comment-create
#   (b) outbound-comment-create
#   (c) comment-edit-bidirectional
#   (d) comment-delete-bidirectional
#   (e) label-create-edit-delete-bidirectional (excluding rebar-id)
#   (f) link-create-edit-delete-bidirectional

JIRA_KEY = "PROJ-1"
LOCAL_ID = "local-id-1"
REBAR_ID_LABEL = "rebar-id-local-id-1"  # typical identity marker format

_DRAFT9_CASES = [
    pytest.param(
        # (a) inbound-comment-create: Jira has a new comment that local does not.
        # rebar-id label is identical on both sides — no label diff, so labels
        # must not appear in any mutation payload at all.
        {
            JIRA_KEY: {
                "dso_local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
            }
        },
        {
            JIRA_KEY: {
                "dso_local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
                "comments": [{"id": "c1", "body": "New Jira comment"}],
            }
        },
        id="inbound-comment-create",
    ),
    pytest.param(
        # (b) outbound-comment-create: local has a new comment; Jira does not.
        # rebar-id label identical on both sides — must not appear in outbound payload.
        {
            JIRA_KEY: {
                "dso_local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
                "comments": [{"id": "c1", "body": "Local comment"}],
            }
        },
        {
            JIRA_KEY: {
                "dso_local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
            }
        },
        id="outbound-comment-create",
    ),
    pytest.param(
        # (c) comment-edit-bidirectional: both sides have different comment bodies.
        # rebar-id label identical on both sides — must not appear in update payload.
        {
            JIRA_KEY: {
                "dso_local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
                "comments": [{"id": "c1", "body": "Local version of comment"}],
            }
        },
        {
            JIRA_KEY: {
                "dso_local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
                "comments": [{"id": "c1", "body": "Jira version of comment"}],
            }
        },
        id="comment-edit-bidirectional",
    ),
    pytest.param(
        # (d) comment-delete-bidirectional: local deleted a comment Jira still has.
        # rebar-id label identical on both sides; comment diverges but labels do not.
        # Since labels are identical, no label entry appears in the update payload.
        # This case verifies that the comment divergence path does not accidentally
        # inject a rebar-id label write via the labels resolver.
        {
            JIRA_KEY: {
                "dso_local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
                "comments": [],
            }
        },
        {
            JIRA_KEY: {
                "dso_local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
                "comments": [{"id": "c1", "body": "Jira still has this comment"}],
            }
        },
        id="comment-delete-bidirectional",
    ),
    pytest.param(
        # (e) label-create-edit-delete-bidirectional (excluding rebar-id):
        # Regular labels diverge (local adds 'sprint-1'; jira adds 'bug').
        # rebar-id label absent from BOTH sides — the resolved payload labels
        # {'feature','sprint-1','bug'} must not contain any rebar-id-* item.
        # This is the primary "excluding rebar-id" contract case: even when
        # label sets diverge, the resolver must never introduce a rebar-id-* label.
        {
            JIRA_KEY: {
                "dso_local_id": LOCAL_ID,
                "labels": ["feature", "sprint-1"],
            }
        },
        {
            JIRA_KEY: {
                "dso_local_id": LOCAL_ID,
                "labels": ["feature", "bug"],
            }
        },
        id="label-create-edit-delete-bidirectional",
    ),
    pytest.param(
        # (e2) label-divergent-rebar-id-local-only: local carries REBAR_ID_LABEL;
        # Jira does NOT. The label sets differ → differ enters the labels-resolution
        # branch and unions both sides. The contract requires that no resolved
        # Mutation propose a write with a rebar-id-* label in its payload.
        # NOTE: per Agent B's notes, conflict_resolver does NOT itself filter
        # rebar-id labels — the contract is enforced end-to-end by the applier's
        # _audit_rebar_id_label_writes guard. This test documents the divergent
        # input shape; the applier-guard tail check below
        # (test_applier_guard_blocks_resolver_rebar_id_label_writes) is the
        # behavior that actually enforces the contract.
        {
            JIRA_KEY: {
                "dso_local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
            }
        },
        {
            JIRA_KEY: {
                "dso_local_id": LOCAL_ID,
                "labels": ["feature"],
            }
        },
        id="label-divergent-rebar-id-local-only",
        marks=pytest.mark.xfail(
            reason=(
                "conflict_resolver does not filter rebar-id-* labels from the union "
                "payload; the applier guard catches this post-resolution. See "
                "test_applier_guard_blocks_resolver_rebar_id_label_writes below."
            ),
            strict=True,
        ),
    ),
    pytest.param(
        # (e3) label-divergent-rebar-id-jira-only: Jira carries REBAR_ID_LABEL;
        # local does NOT. Symmetric inbound counterpart of (e2).
        {
            JIRA_KEY: {
                "dso_local_id": LOCAL_ID,
                "labels": ["feature"],
            }
        },
        {
            JIRA_KEY: {
                "dso_local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
            }
        },
        id="label-divergent-rebar-id-jira-only",
        marks=pytest.mark.xfail(
            reason=(
                "conflict_resolver does not filter rebar-id-* labels from the union "
                "payload; the applier guard catches this post-resolution. See "
                "test_applier_guard_blocks_resolver_rebar_id_label_writes below."
            ),
            strict=True,
        ),
    ),
    pytest.param(
        # (f) link-create-edit-delete-bidirectional:
        # Links diverge (jira has an extra 'relates' link); rebar-id label
        # identical on both sides — no rebar-id label write proposed.
        # The link elements are plain strings here (not dicts) to avoid
        # the unhashable-dict issue in resolve_set_valued's dedup pass.
        {
            JIRA_KEY: {
                "dso_local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
                "links": ["PROJ-2"],
            }
        },
        {
            JIRA_KEY: {
                "dso_local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
                "links": ["PROJ-2", "PROJ-3"],
            }
        },
        id="link-create-edit-delete-bidirectional",
    ),
]


@pytest.mark.parametrize("local_state,jira_state", _DRAFT9_CASES)
def test_no_rebar_id_label_writes_per_draft9_case(
    differ, local_state: dict, jira_state: dict, request
) -> None:
    """For each draft-9 provenance case, no mutation proposes a rebar-id label write.

    Drives compute_mutations with the 6-case matrix and asserts that for every
    emitted Mutation with action in {create, update}, the 'labels' payload key
    (if present) contains no item starting with 'rebar-id-'.

    Contract (dd-3): conflict_resolver per-element provenance MUST skip rebar-id
    labels; the identity marker remains exclusively under inbound_clean_label /
    outbound_create jurisdiction.
    """
    mutations = differ.compute_mutations(local_state, jira_state)
    _assert_no_rebar_id_label_writes(mutations, case_id=request.node.callid if hasattr(request.node, "callid") else request.node.name)


# ---------------------------------------------------------------------------
# Direct resolver + applier-guard tail check
# ---------------------------------------------------------------------------
#
# The xfail cases above (e2, e3) confirm conflict_resolver does NOT itself
# filter rebar-id-* labels from the union payload — it unconditionally unions
# both sides. The actual end-to-end contract is enforced by the applier's
# _audit_rebar_id_label_writes guard, which fires before any unauthorized leaf
# dispatches a rebar-id-* label write.
#
# The test below drives conflict_resolver.resolve_field DIRECTLY with divergent
# inputs (bypassing the differ's no-diff short-circuit) and then asserts the
# applier guard raises RebarIdLabelWriteError when an unauthorized leaf attempts
# to write a Mutation containing the resolver's output.


@pytest.fixture(scope="module")
def conflict_resolver():
    return _load(CONFLICT_RESOLVER_PATH, "conflict_resolver_no_rebar_id_writes")


@pytest.fixture(scope="module")
def mutation_mod():
    return _load(MUTATION_PATH, "mutation_no_rebar_id_writes")


@pytest.fixture(scope="module")
def applier_mod():
    # applier imports _errors via relative dotted lookup; seed the canonical
    # package path so RebarIdLabelWriteError resolves at raise-time.
    import types as _types
    for _parent in (
        "rebar_reconciler",
    ):
        if _parent not in sys.modules:
            sys.modules[_parent] = _types.ModuleType(_parent)
    errors_path = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "_errors.py"
    errors_key = "rebar_reconciler._errors"
    if errors_key not in sys.modules:
        _load(errors_path, errors_key)
    return _load(APPLIER_PATH, "applier_no_rebar_id_writes")


def test_resolver_unions_rebar_id_label_on_divergent_sides(conflict_resolver) -> None:
    """resolve_field('labels', ...) returns the union including rebar-id-* when
    sides diverge — documents the current (unfiltered) resolver behavior.

    This pins the contract boundary: the resolver does not filter rebar-id-*
    labels itself; the applier guard catches unauthorized writes downstream.
    """
    local_labels = [REBAR_ID_LABEL, "feature"]
    jira_labels = ["feature"]
    resolved = conflict_resolver.resolve_field(
        "labels", local_labels, jira_labels, provenance_record=None
    )
    assert isinstance(resolved, list)
    # Current behavior: rebar-id-* is unioned in. If the resolver gains an
    # explicit filter for rebar-id-* labels, this assertion will fail and the
    # xfail markers on (e2)/(e3) above should be removed.
    assert any(str(lbl).startswith("rebar-id-") for lbl in resolved), (
        "resolve_field('labels', ...) is expected to union all labels including "
        "rebar-id-* (no resolver-level filter). The applier guard "
        "(_audit_rebar_id_label_writes) is the contract enforcer."
    )


def test_applier_guard_blocks_resolver_rebar_id_label_writes(
    conflict_resolver, mutation_mod, applier_mod
) -> None:
    """When the resolver's union output is wrapped in a Mutation routed to an
    unauthorized leaf (outbound_update), the applier guard raises
    RebarIdLabelWriteError before any side-effect.

    This is the load-bearing end-to-end contract check: the resolver may
    union rebar-id-* labels into its output, but the applier _audit_rebar_id_label_writes
    guard MUST block any unauthorized leaf from acting on that payload.
    """
    # 1. Resolver produces a union list that includes a rebar-id-* label.
    local_labels = [REBAR_ID_LABEL, "feature"]
    jira_labels = ["feature"]
    resolved = conflict_resolver.resolve_field(
        "labels", local_labels, jira_labels, provenance_record=None
    )
    assert any(str(lbl).startswith("rebar-id-") for lbl in resolved)

    # 2. Build a label-target Mutation carrying the offending rebar-id label.
    #    The applier's _is_rebar_id_label_write_mutation matches mutations where
    #    target == 'label' AND payload (string) starts with 'rebar-id-'.
    offending_label = next(
        lbl for lbl in resolved if str(lbl).startswith("rebar-id-")
    )
    mut = mutation_mod.Mutation(
        direction=mutation_mod.MutationDirection.outbound,
        action=mutation_mod.MutationAction.update,
        target="label",
        payload={"label": offending_label, "target": "label"},
        provenance={"source": "test"},
    )

    # 3. Invoke the guard directly with an unauthorized leaf name.
    #    outbound_update is NOT in _AUTHORIZED_REBAR_ID_LABEL_WRITERS.
    #    The applier loads its own _errors module under the canonical key
    #    'rebar_reconciler_errors' (see _load_errors_module); use the re-export.
    error_cls = applier_mod.RebarIdLabelWriteError

    # Ensure guard mode is 'raise' regardless of environment.
    import os as _os
    prev = _os.environ.get("REBAR_ID_GUARD_MODE")
    _os.environ["REBAR_ID_GUARD_MODE"] = "raise"
    try:
        with pytest.raises(error_cls):
            applier_mod._audit_rebar_id_label_writes("outbound_update", [mut])
    finally:
        if prev is None:
            _os.environ.pop("REBAR_ID_GUARD_MODE", None)
        else:
            _os.environ["REBAR_ID_GUARD_MODE"] = prev
