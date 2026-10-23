"""Invoke a post-deletion leaf and check an observable result. The sibling linkage test
only checks the registry; this test exposes import and dispatch failures.
"""

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "applier.py"


@pytest.fixture(scope="module")
def applier():
    return importlib.import_module("rebar.reconciler.applier")


def test_outbound_probe_leaf_genuinely_invocable(applier):
    """Invoke the read-only outbound probe through its real import and dispatch chain.
    Assert the fetched issue, direction, action, and client key; propagate any
    error.
    """
    mut_mod = applier._load_mutation_module()
    direction = mut_mod.MutationDirection.outbound
    action = mut_mod.MutationAction.probe
    leaf = applier._LEAVES[(direction, action)]

    issue = {"key": "MOCK-1", "fields": {"summary": "hello"}}
    client = SimpleNamespace(get_issue=MagicMock(return_value=issue))
    mutation = mut_mod.Mutation(
        direction=direction,
        action=action,
        target="MOCK-1",
        payload={},
        provenance={"source": "smoke"},
    )

    result = leaf(mutation, client=client)

    # Observable behavior: the leaf returns a probe verdict derived from the
    # live client response, not a swallowed error.
    assert result.direction == direction
    assert result.action == action
    assert result.payload == {"present": True, "issue": issue}
    # The leaf genuinely reached the transport with the target key.
    client.get_issue.assert_called_once_with("MOCK-1")
