"""Post-deletion registry-LINKAGE guard (task 76e6 / story 2da6).

Renamed from the former ``test_post_deletion_smoke.py``, which was mis-named: it
looked like an invocability proof but actually only asserted stale-import/linkage
survival. This file is scoped to that LINKAGE concern ONLY — that the applier's
typed-dispatch registry (``_LEAVES``) still imports cleanly and exposes exactly the
expected leaves after the legacy bridge deletion (task 6cbb) and the bridge-cutover
one-shot removal (WS4). It is NOT an invocability proof — that lives in the sibling
``test_leaves_invocable.py``, which actually invokes a leaf and asserts an observable
outcome.
"""

import importlib.util
import sys
from pathlib import Path

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
