"""Post-deletion smoke test (task 76e6).

Verifies that with the legacy bridge modules removed (task 6cbb) and the
bridge-cutover one-shots removed (WS4), applier._LEAVES still loads and every
leaf is invocable without ModuleNotFoundError.
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
    # Register in sys.modules so dataclass __module__ resolution works.
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def applier():
    return _load_module(APPLIER_PATH, "applier")


def test_all_leaves_invocable_without_import_error(applier):
    """Every (direction, action) entry in _LEAVES is invocable without
    ModuleNotFoundError after the legacy bridge deletion (task 6cbb)."""
    mut_mod = applier._load_mutation_module()
    # Construct a permissive mock client whose methods all return None.
    client = SimpleNamespace(
        create_issue=MagicMock(return_value={"key": "MOCK-1"}),
        update_issue=MagicMock(return_value=None),
        delete_issue=MagicMock(return_value=None),
        remove_label=MagicMock(return_value=None),
        add_label=MagicMock(return_value=None),
        get_issue=MagicMock(return_value={"key": "MOCK-1", "fields": {}}),
        search_issues=MagicMock(return_value={"issues": [], "total": 0}),
    )
    leaves_count = 0
    for (direction, action), leaf in applier._LEAVES.items():
        leaves_count += 1
        mutation = mut_mod.Mutation(
            direction=direction,
            action=action,
            target="MOCK-1",
            payload={"changed_fields": {"title": "x"}, "labels_to_remove": ["rebar-id-a"]},
            provenance={"source": "smoke"},
        )
        # Each leaf must be callable; ModuleNotFoundError on import would
        # already have fired by the time _LEAVES was constructed.
        try:
            leaf(mutation, client=client)
        except ModuleNotFoundError as e:
            pytest.fail(f"Leaf ({direction.value}, {action.value}) raised ModuleNotFoundError: {e}")
        except Exception:
            # Other exceptions are tolerated — the smoke test only catches
            # import-level regressions. Stubs may legitimately raise on
            # missing payload fields, etc.
            pass
    assert leaves_count >= 6, f"_LEAVES has only {leaves_count} entries — expected at least 6 valid pairs"
