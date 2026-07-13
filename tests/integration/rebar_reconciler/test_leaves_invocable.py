"""Post-deletion leaf INVOCABILITY guard (task 76e6 / story 2da6).

Complements the registry-linkage guard in ``test_post_deletion_linkage.py``: this
file actually INVOKES a post-deletion leaf and asserts an observable outcome, so it
is a genuine invocability proof (not merely a linkage/import check). Nothing is
swallowed — a real import/dispatch error fails the test.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"


def _load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def applier():
    return _load_module(APPLIER_PATH, "applier")


def test_outbound_probe_leaf_genuinely_invocable(applier):
    """A post-deletion leaf is INVOCABLE end-to-end, proven by an observable outcome.

    The outbound ``probe`` leaf is chosen because it is read-only (a single
    ``get_issue`` lookup, no local writes / rollback): invoking it exercises the
    real import chain, ``_direction_guard``, and ``_call_with_retry`` after the
    legacy bridge deletion. We assert the OBSERVABLE post-conditions — the leaf
    reports the issue as present, echoes the fetched issue into its ``ApplyResult``
    payload, tags the result with the right direction/action, and actually calls
    the client with the target key. Any real error (import, dispatch, etc.) is
    allowed to fail the test rather than being swallowed.
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
