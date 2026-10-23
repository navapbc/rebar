"""Check post-deletion typed-dispatch registry linkage: expected leaves still import
and appear in _LEAVES. Actual leaf invocation is covered by
test_leaves_invocable.py.
"""

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "applier.py"

# The exact (direction, action) pairs the registry must expose after the legacy
# bridge deletion. Pinned here so a leaf silently dropped from the routing table
# fails this guard instead of passing a loose ">= N" count.
EXPECTED_LEAF_PAIRS = frozenset(
    {
        ("inbound", "clean_label"),
        ("inbound", "conflict"),
        ("inbound", "create"),
        # Inbound probe is unreachable, and inbound delete would resurrect a Jira
        # deletion. Outbound probe remains live for bounded absence checks.
        ("inbound", "repair_property"),
        ("inbound", "update"),
        ("outbound", "conflict"),
        ("outbound", "create"),
        ("outbound", "delete"),
        ("outbound", "probe"),
        ("outbound", "update"),
    }
)


@pytest.fixture(scope="module")
def applier():
    return importlib.import_module("rebar.reconciler.applier")


def test_leaves_registry_linkage_intact(applier):
    """The registry contains exactly the expected direction/action pairs and callable
    leaves; import failure breaks module load. Invocation is checked separately.
    """
    registered = {(d.value, a.value) for (d, a) in applier._LEAVES}
    assert registered == set(EXPECTED_LEAF_PAIRS), (
        f"registry drift: extra={registered - EXPECTED_LEAF_PAIRS} "
        f"missing={set(EXPECTED_LEAF_PAIRS) - registered}"
    )
    for key, leaf in applier._LEAVES.items():
        assert callable(leaf), f"leaf for {key} is not callable: {leaf!r}"
