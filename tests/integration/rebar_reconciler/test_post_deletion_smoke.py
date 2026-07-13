"""Post-deletion invocability + registry-linkage guards (task 76e6).

With the legacy bridge modules removed (task 6cbb) and the bridge-cutover
one-shots removed (WS4), the applier's typed-dispatch registry (``_LEAVES``)
must still import cleanly and its leaves must still be genuinely invocable.

Two guards, each asserting OBSERVABLE behavior:

  - test_outbound_probe_leaf_genuinely_invocable: actually invokes a
    post-deletion leaf (the read-only outbound ``probe``) against a mock Jira
    client and asserts its observable post-conditions (the returned
    ``ApplyResult`` payload + that the client was called). Real errors are
    allowed to fail the test — nothing is swallowed.
  - test_leaves_registry_linkage_intact: a REGISTRY-LINKAGE guard (not an
    invocability proof) — asserts the exact set of (direction, action) pairs
    survived the deletion and that every registered value is callable.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"

# The exact (direction, action) pairs the registry must expose after the legacy
# bridge deletion. Pinned here so a leaf silently dropped from the routing table
# fails this guard instead of passing a loose ">= N" count.
EXPECTED_LEAF_PAIRS = frozenset(
    {
        ("inbound", "clean_label"),
        ("inbound", "conflict"),
        ("inbound", "create"),
        ("inbound", "delete"),
        ("inbound", "probe"),
        ("inbound", "repair_property"),
        ("inbound", "update"),
        ("outbound", "conflict"),
        ("outbound", "create"),
        ("outbound", "delete"),
        ("outbound", "probe"),
        ("outbound", "update"),
    }
)


def _load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    # Register in sys.modules so dataclass __module__ resolution works.
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


def test_leaves_registry_linkage_intact(applier):
    """Registry-linkage guard: the typed-dispatch table survived the legacy bridge
    deletion (task 6cbb) with the expected leaves still wired and callable.

    This is a LINKAGE check, not an invocability proof — it asserts the observable
    shape of the public ``_LEAVES`` registry: the exact (direction, action) pairs
    are present (so no leaf was silently added or dropped) and every registered
    value is callable. A missing import behind any leaf would already have raised
    ``ModuleNotFoundError`` at module load (the fixture), failing this test.
    """
    registered = {(d.value, a.value) for (d, a) in applier._LEAVES}
    assert registered == set(EXPECTED_LEAF_PAIRS), (
        f"registry drift: extra={registered - EXPECTED_LEAF_PAIRS} "
        f"missing={set(EXPECTED_LEAF_PAIRS) - registered}"
    )
    for key, leaf in applier._LEAVES.items():
        assert callable(leaf), f"leaf for {key} is not callable: {leaf!r}"
