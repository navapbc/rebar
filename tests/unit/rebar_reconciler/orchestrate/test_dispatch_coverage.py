"""Enumerative dispatch-coverage tests for reconcile._dispatch_mutation.

Verifies that every (direction, action) pair in mutation._VALID_COMBINATIONS
is routed by _dispatch_mutation, that each leaf routes to a distinct callable,
and that an unknown pair raises NotImplementedError naming the tuple.

Test-loading strategy
---------------------
Follows the established importlib convention for this test directory (see
conftest.py docstring and test_reconcile_main.py).  Modules under test are
loaded via ``importlib.util.spec_from_file_location`` rather than ordinary
``import`` statements to avoid sys.path manipulation and keep each test
self-contained.

The module-scoped ``_load_reconciler_modules`` fixture:
  1. Loads mutation.py under the short key ``reconcile_mutation`` (the same
     key reconcile.py uses internally via _load("reconcile_mutation", ...)).
  2. Loads reconcile.py under the key ``reconcile_dispatch_test``.
  3. Resets reconcile._DISPATCH_TABLE to None before each test so the lazy
     table is rebuilt fresh and import-side effects from prior tests don't
     contaminate coverage assertions.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
_PKG_DIR = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"
_MUTATION_PATH = _PKG_DIR / "mutation.py"
_RECONCILE_PATH = _PKG_DIR / "reconcile.py"

# Module-registry keys used internally by reconcile.py
_MUTATION_KEY = "reconcile_mutation"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_module(name: str, path: Path) -> types.ModuleType:
    """Load a file as a named module and register it in sys.modules."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"Cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Module-scoped fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _load_reconciler_modules(request):
    """Load mutation + reconcile modules; register in sys.modules.

    Returns a dict with keys 'mutation_mod' and 'reconcile_mod'.
    Registers a finalizer to remove the keys added by this fixture.
    """
    newly_added: list[str] = []

    # Load mutation.py under the key reconcile.py uses internally.
    mutation_mod = _load_module(_MUTATION_KEY, _MUTATION_PATH)
    newly_added.append(_MUTATION_KEY)

    # Also seed the rebar_reconciler_mutation key used by applier.py.
    applier_mut_key = "rebar_reconciler_mutation"
    if applier_mut_key not in sys.modules:
        sys.modules[applier_mut_key] = mutation_mod
        newly_added.append(applier_mut_key)

    # Load reconcile.py under a test-private key.
    reconcile_key = "reconcile_dispatch_test"
    reconcile_mod = _load_module(reconcile_key, _RECONCILE_PATH)
    newly_added.append(reconcile_key)

    def _cleanup():
        for key in newly_added:
            sys.modules.pop(key, None)

    request.addfinalizer(_cleanup)

    return {"mutation_mod": mutation_mod, "reconcile_mod": reconcile_mod}


@pytest.fixture
def mutation_mod(_load_reconciler_modules):
    return _load_reconciler_modules["mutation_mod"]


@pytest.fixture
def reconcile_mod(_load_reconciler_modules):
    """Return the reconcile module with _DISPATCH_TABLE reset to None.

    Resetting to None forces _build_dispatch_table() to run fresh for each
    test, so import-side-effects from prior tests cannot leak coverage state.
    """
    mod = _load_reconciler_modules["reconcile_mod"]
    mod._DISPATCH_TABLE = None
    return mod


# ---------------------------------------------------------------------------
# Minimal Mutation stub
# ---------------------------------------------------------------------------


def _make_mutation(direction_val: str, action_val: str, target: str = "PROJ-1"):
    """Build a duck-typed Mutation stub for a given (direction, action) pair.

    Returns an object with ``.direction`` and ``.action`` attributes whose
    ``.value`` property returns the string value — matching MutationDirection /
    MutationAction StrEnum behaviour.
    """

    class _Val:
        def __init__(self, v: str) -> None:
            self.value = v

        def __str__(self) -> str:
            return self.value

        def __eq__(self, other: Any) -> bool:
            if isinstance(other, _Val):
                return self.value == other.value
            return self.value == str(other)

        def __hash__(self) -> int:
            return hash(self.value)

    class _Mutation:
        def __init__(self, d: str, a: str, t: str) -> None:
            self.direction = _Val(d)
            self.action = _Val(a)
            self.target = t
            self.payload: dict = {}
            self.provenance: dict = {}

    return _Mutation(direction_val, action_val, target)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAllDirectionActionPairsCovered:
    """test_all_direction_action_pairs_covered

    For every (direction, action) pair in the cartesian product
    {inbound, outbound} × MutationAction, the pair must either:
      (a) be dispatched by _dispatch_mutation without raising, OR
      (b) be explicitly listed in reconcile.INVALID_PAIRS.

    The union of (a) and (b) must equal the full cartesian product.
    """

    def test_all_direction_action_pairs_covered(self, mutation_mod, reconcile_mod):
        D = mutation_mod.MutationDirection
        A = mutation_mod.MutationAction

        cartesian = {
            (str(d.value), str(a.value))
            for d in D
            for a in A
        }

        invalid_strs: set[tuple[str, str]] = {
            (str(d), str(a)) for d, a in reconcile_mod.INVALID_PAIRS
        }

        dispatched: set[tuple[str, str]] = set()
        dispatch_errors: list[str] = []

        for d_val, a_val in cartesian:
            if (d_val, a_val) in invalid_strs:
                continue
            mut = _make_mutation(d_val, a_val)
            try:
                reconcile_mod._dispatch_mutation(mut, context=None)
                dispatched.add((d_val, a_val))
            except NotImplementedError:
                dispatch_errors.append(f"({d_val!r}, {a_val!r})")
            except Exception:
                # Any other exception from a stub leaf counts as dispatched —
                # the routing reached the leaf; the leaf may raise for other
                # reasons (missing client, etc.).
                dispatched.add((d_val, a_val))

        covered = dispatched | invalid_strs
        missing = cartesian - covered

        assert not dispatch_errors, (
            f"_dispatch_mutation raised NotImplementedError for valid pairs: "
            f"{dispatch_errors}"
        )
        assert missing == set(), (
            f"Pairs not covered by dispatch table or INVALID_PAIRS: {missing}"
        )
        assert covered == cartesian, (
            f"covered ({len(covered)}) != cartesian ({len(cartesian)})"
        )


class TestEachLeafRoutesToDistinctApplier:
    """test_each_leaf_routes_to_distinct_applier

    The _DISPATCH_TABLE, once built, maps each registered (d, a) key to a
    distinct callable object (no two pairs share the same leaf).
    """

    def test_each_leaf_routes_to_distinct_applier(self, mutation_mod, reconcile_mod):
        # Force table to build.
        D = mutation_mod.MutationDirection
        A = mutation_mod.MutationAction

        # Trigger build of dispatch table by calling _dispatch_mutation once.
        first_d = list(D)[0].value
        first_a = list(A)[0].value
        try:
            reconcile_mod._dispatch_mutation(_make_mutation(first_d, first_a))
        except Exception:
            # Intentionally broad: this call's ONLY purpose is to populate the
            # lazy-built _DISPATCH_TABLE as a side effect. The first
            # (direction, action) pair may legitimately raise any of:
            #   - NotImplementedError (leaf is a stub)
            #   - rebar_reconciler_errors.DirectionMismatchError (guard fires
            #     before the test mutation reaches a real handler)
            #   - LookupError / AttributeError / TypeError (intentionally
            #     minimal _make_mutation stub lacks production fields)
            # All of these still cause _DISPATCH_TABLE to be populated, which
            # is what the assertions below validate. Anything fatal at module
            # import time (e.g., ImportError) propagates because it happens
            # before _dispatch_mutation is entered.
            pass

        assert reconcile_mod._DISPATCH_TABLE is not None, (
            "_DISPATCH_TABLE must be populated after first _dispatch_mutation call"
        )

        table = reconcile_mod._DISPATCH_TABLE
        assert len(table) > 0, "_DISPATCH_TABLE must have at least one entry"

        callables = list(table.values())
        callable_ids = [id(c) for c in callables]

        assert len(callable_ids) == len(set(callable_ids)), (
            f"Duplicate leaf callables detected in _DISPATCH_TABLE — "
            f"each (direction, action) pair must route to a distinct callable. "
            f"Table keys: {list(table.keys())}"
        )


class TestUnknownPairRaisesNamedNotImplemented:
    """test_unknown_pair_raises_named_not_implemented

    Passing an unknown (direction, action) pair to _dispatch_mutation must
    raise NotImplementedError with a message that names the tuple.
    """

    def test_unknown_pair_raises_named_not_implemented(self, reconcile_mod):
        fake_mut = _make_mutation("inbound", "nonexistent_action_xyz")

        with pytest.raises(NotImplementedError) as exc_info:
            reconcile_mod._dispatch_mutation(fake_mut, context=None)

        msg = str(exc_info.value)
        assert "nonexistent_action_xyz" in msg, (
            f"NotImplementedError message must name the unknown action; got: {msg!r}"
        )

    def test_unknown_direction_raises_named_not_implemented(self, reconcile_mod):
        fake_mut = _make_mutation("sideways", "create")

        with pytest.raises(NotImplementedError) as exc_info:
            reconcile_mod._dispatch_mutation(fake_mut, context=None)

        msg = str(exc_info.value)
        assert "sideways" in msg, (
            f"NotImplementedError message must name the unknown direction; got: {msg!r}"
        )
