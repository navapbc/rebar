"""Draft-9 comment, label, and link cases forbid rebar-id label writes in create or update
mutations. The differ suppresses identity-label-only drift; the applier guard blocks
unauthorized writes from direct resolver output."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Module loading (per conftest.py convention for this directory)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
DIFFER_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "differ.py"
CONFLICT_RESOLVER_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "conflict_resolver.py"
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "applier.py"
MUTATION_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "mutation.py"


@pytest.fixture(scope="module")
def differ():
    return importlib.import_module("rebar.reconciler.differ")


# ---------------------------------------------------------------------------
# Assertion helper
# ---------------------------------------------------------------------------

_WRITE_ACTIONS = {"create", "update"}


def _assert_no_rebar_id_label_writes(mutations: list[Any], case_id: str) -> None:
    """Reject create or update mutations whose labels payload contains a rebar-id identity
    marker."""
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


# Draft-9 cases vary comments, labels, and links while forbidding rebar-id writes.
# Identity-label-only drift is filtered before resolver dispatch.

JIRA_KEY = "PROJ-1"
LOCAL_ID = "local-id-1"
REBAR_ID_LABEL = "rebar-id-local-id-1"  # typical identity marker format

_DRAFT9_CASES = [
    pytest.param(
        # (a) Jira adds a comment; unchanged identity labels produce no label payload.
        {
            JIRA_KEY: {
                "local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
            }
        },
        {
            JIRA_KEY: {
                "local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
                "comments": [{"id": "c1", "body": "New Jira comment"}],
            }
        },
        id="inbound-comment-create",
    ),
    pytest.param(
        # (b) Local adds a comment; unchanged identity labels stay out of outbound payloads.
        {
            JIRA_KEY: {
                "local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
                "comments": [{"id": "c1", "body": "Local comment"}],
            }
        },
        {
            JIRA_KEY: {
                "local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
            }
        },
        id="outbound-comment-create",
    ),
    pytest.param(
        # (c) Both comments differ; unchanged identity labels stay out of updates.
        {
            JIRA_KEY: {
                "local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
                "comments": [{"id": "c1", "body": "Local version of comment"}],
            }
        },
        {
            JIRA_KEY: {
                "local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
                "comments": [{"id": "c1", "body": "Jira version of comment"}],
            }
        },
        id="comment-edit-bidirectional",
    ),
    pytest.param(
        # (d) Local deletes a comment; the label resolver must not add an identity-label write.
        {
            JIRA_KEY: {
                "local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
                "comments": [],
            }
        },
        {
            JIRA_KEY: {
                "local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
                "comments": [{"id": "c1", "body": "Jira still has this comment"}],
            }
        },
        id="comment-delete-bidirectional",
    ),
    pytest.param(
        # (e) Ordinary labels diverge without an identity label; their union stays identity-free.
        {
            JIRA_KEY: {
                "local_id": LOCAL_ID,
                "labels": ["feature", "sprint-1"],
            }
        },
        {
            JIRA_KEY: {
                "local_id": LOCAL_ID,
                "labels": ["feature", "bug"],
            }
        },
        id="label-create-edit-delete-bidirectional",
    ),
    pytest.param(
        # (e2) Local-only identity-label drift is an echo suppressed by the differ.
        # The applier guard still protects direct resolver output.
        {
            JIRA_KEY: {
                "local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
            }
        },
        {
            JIRA_KEY: {
                "local_id": LOCAL_ID,
                "labels": ["feature"],
            }
        },
        id="label-divergent-rebar-id-local-only",
    ),
    pytest.param(
        # (e3) Jira-only identity-label drift is symmetrically suppressed by the differ.
        {
            JIRA_KEY: {
                "local_id": LOCAL_ID,
                "labels": ["feature"],
            }
        },
        {
            JIRA_KEY: {
                "local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
            }
        },
        id="label-divergent-rebar-id-jira-only",
    ),
    pytest.param(
        # (f) Links diverge while identity labels match; plain string links keep set union valid.
        {
            JIRA_KEY: {
                "local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
                "links": ["PROJ-2"],
            }
        },
        {
            JIRA_KEY: {
                "local_id": LOCAL_ID,
                "labels": [REBAR_ID_LABEL, "feature"],
                "links": ["PROJ-2", "PROJ-3"],
            }
        },
        id="link-create-edit-delete-bidirectional",
    ),
]


@pytest.mark.parametrize(("local_state", "jira_state"), _DRAFT9_CASES)
def test_no_rebar_id_label_writes_per_draft9_case(
    differ, local_state: dict, jira_state: dict, request
) -> None:
    """Across the draft-9 provenance matrix, no create or update mutation may write a
    rebar-id identity label."""
    mutations = differ.compute_mutations(local_state, jira_state)
    _assert_no_rebar_id_label_writes(
        mutations,
        case_id=request.node.callid if hasattr(request.node, "callid") else request.node.name,
    )


# Direct resolver union still includes identity labels; the applier guard blocks
# unauthorized writes before dispatch. Bypass the differ here to test that boundary.


@pytest.fixture(scope="module")
def conflict_resolver():
    return importlib.import_module("rebar.reconciler.conflict_resolver")


@pytest.fixture(scope="module")
def mutation_mod():
    return importlib.import_module("rebar.reconciler.mutation")


@pytest.fixture(scope="module")
def applier_mod():
    return importlib.import_module("rebar.reconciler.applier")


def test_resolver_unions_rebar_id_label_on_divergent_sides(conflict_resolver) -> None:
    """The resolver still unions identity labels; the applier guard is the authorized-writer
    boundary."""
    local_labels = [REBAR_ID_LABEL, "feature"]
    jira_labels = ["feature"]
    resolved = conflict_resolver.resolve_field(
        "labels", local_labels, jira_labels, provenance_record=None
    )
    assert isinstance(resolved, list)
    # The resolver currently unions identity labels; the applier enforces write authority.
    assert any(str(lbl).startswith("rebar-id-") for lbl in resolved), (
        "resolve_field('labels', ...) is expected to union all labels including "
        "rebar-id-* (no resolver-level filter). The applier guard "
        "(_audit_rebar_id_label_writes) is the contract enforcer."
    )


def test_applier_guard_blocks_resolver_rebar_id_label_writes(
    conflict_resolver, mutation_mod, applier_mod
) -> None:
    """An unauthorized outbound leaf cannot dispatch a resolver-produced rebar-id label
    write; the applier guard raises first."""
    # 1. Resolver output includes the identity label.
    local_labels = [REBAR_ID_LABEL, "feature"]
    jira_labels = ["feature"]
    resolved = conflict_resolver.resolve_field(
        "labels", local_labels, jira_labels, provenance_record=None
    )
    assert any(str(lbl).startswith("rebar-id-") for lbl in resolved)

    # 2. Build a label-target mutation with that identity label for the guard.
    offending_label = next(lbl for lbl in resolved if str(lbl).startswith("rebar-id-"))
    mut = mutation_mod.Mutation(
        direction=mutation_mod.MutationDirection.outbound,
        action=mutation_mod.MutationAction.update,
        target="label",
        payload={"label": offending_label, "target": "label"},
        provenance={"source": "test"},
    )

    # 3. An outbound_update leaf lacks authority to write it; use the exported guard error.
    error_cls = applier_mod.RebarIdLabelWriteError

    # Force guard rejection.
    import os as _os

    prev = _os.environ.get("REBAR_UNSAFE_ID_GUARD_BYPASS")
    _os.environ["REBAR_UNSAFE_ID_GUARD_BYPASS"] = "false"
    try:
        with pytest.raises(error_cls):
            applier_mod._audit_rebar_id_label_writes("outbound_update", [mut])
    finally:
        if prev is None:
            _os.environ.pop("REBAR_UNSAFE_ID_GUARD_BYPASS", None)
        else:
            _os.environ["REBAR_UNSAFE_ID_GUARD_BYPASS"] = prev
