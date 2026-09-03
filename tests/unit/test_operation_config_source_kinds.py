"""The oracle for ``_operation_config._source_kinds`` (ticket 70f5-2253-1671-474d).

``rebar._operation_config`` used to hand-copy the five precedence layer labels that
``rebar.config.LAYER_ORDER`` already declares. These tests pin the replacement: a
DERIVED vocabulary, computed behind a cached accessor whose import of ``rebar.config``
is deferred — deferred because a module-scope import would close an import cycle
(``rebar.config`` imports ``ENVELOPE_VERSION`` / ``OperationSnapshot`` / ``active_snapshot``
FROM ``rebar._operation_config``, which is the leaf).
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

from rebar import _operation_config
from rebar import config as rebar_config

pytestmark = pytest.mark.unit

_MODULE_PATH = Path(_operation_config.__file__)
_MODULE_TREE = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def clean_source_kinds_cache():
    """Opt-in for the one test that perturbs ``LAYER_ORDER``.

    Deliberately NOT autouse: only that test can poison the ``functools.cache``, and an
    autouse fixture would be a new always-on mechanism for a single call site."""
    _operation_config._source_kinds.cache_clear()
    yield
    _operation_config._source_kinds.cache_clear()


def _snapshot(sources):
    return _operation_config.OperationSnapshot.build(
        envelope_version=_operation_config.ENVELOPE_VERSION,
        repo_root="/tmp/repo",
        values={"core": {"k": "v"}},
        sources=sources,
    )


# ── AC1: derived, not re-listed ───────────────────────────────────────────────


def test_derived_set_equals_layer_order():
    assert _operation_config._source_kinds() == frozenset(rebar_config.LAYER_ORDER)


def test_the_hand_copied_literal_is_gone():
    """AC1's other half: the five labels are no longer spelled out in the module."""
    source = _MODULE_PATH.read_text(encoding="utf-8")
    copied = ("default", "user", "project", "env", "cli")
    literal_sets = [
        node
        for node in ast.walk(_MODULE_TREE)
        if isinstance(node, (ast.Set, ast.Tuple, ast.List))
        and {
            elt.value
            for elt in node.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        }
        >= set(copied)
    ]
    assert literal_sets == [], "the layer labels are re-listed as a literal collection"
    assert "_SOURCE_KINDS" not in source, "the hand-copied constant is still present"


# ── AC1b: the derivation is DEFERRED ──────────────────────────────────────────


def _module_scope_import_targets() -> set[str]:
    """Every module name imported at the TOP LEVEL of ``_operation_config``."""
    targets: set[str] = set()
    for node in _MODULE_TREE.body:
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            targets.add(node.module)
            targets.update(f"{node.module}.{alias.name}" for alias in node.names)
    return targets


def test_rebar_config_is_not_imported_at_module_scope():
    """The cycle guard: importing ``rebar.config`` in the module body would break config.

    ``rebar/config.py`` imports names FROM this module partway down its own body, and
    ``LAYER_ORDER`` is defined AFTER that point, so a module-scope
    ``from rebar.config import LAYER_ORDER`` here raises ImportError on a cold import
    of ``rebar.config``.
    """
    assert not any(
        target == "rebar.config" or target.startswith("rebar.config.")
        for target in _module_scope_import_targets()
    )


def test_source_kinds_imports_rebar_config_inside_its_own_body():
    """The deferral is real: the import lives inside the accessor, not beside it."""
    (func,) = [
        node
        for node in _MODULE_TREE.body
        if isinstance(node, ast.FunctionDef) and node.name == "_source_kinds"
    ]
    deferred = {
        node.module for node in ast.walk(func) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "rebar.config" in deferred


def test_derivation_does_not_run_until_the_accessor_is_called():
    """A clean interpreter: importing the module performs no derivation.

    (``rebar.config`` is unavoidably in ``sys.modules`` after ANY ``rebar`` submodule
    import, because ``rebar/__init__.py`` imports it — so the observable deferral is
    that the accessor has not yet run, not the absence of the module.)
    """
    program = (
        "import sys\n"
        "import rebar._operation_config as oc\n"
        "assert oc._source_kinds.cache_info().misses == 0, 'derived at import time'\n"
        "assert oc._source_kinds(), 'accessor returned an empty vocabulary'\n"
        "assert oc._source_kinds.cache_info().misses == 1\n"
        "assert oc._source_kinds() is oc._source_kinds(), 'result is not cached'\n"
        "assert oc._source_kinds.cache_info().misses == 1, 'derivation re-ran'\n"
        "print('ok')\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        env=subprocess_env(),
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"


# ── AC2: a new layer arrives with no second edit ──────────────────────────────


def test_a_layer_added_to_layer_order_reaches_the_derived_set(
    monkeypatch, clean_source_kinds_cache
):
    monkeypatch.setattr(
        rebar_config, "LAYER_ORDER", (*rebar_config.LAYER_ORDER, "remote"), raising=True
    )
    assert "remote" in _operation_config._source_kinds()
    # …and the new label is accepted as provenance, with no edit to _operation_config.
    snapshot = _snapshot({"core": {"k": "remote"}})
    assert snapshot.sources["core"]["k"] == "remote"


# ── AC3: no consumer depends on ORDER ─────────────────────────────────────────


def test_every_use_site_treats_the_vocabulary_as_a_membership_set():
    """tuple -> frozenset is lossless only if nothing reads precedence from it."""
    calls = [
        node
        for node in ast.walk(_MODULE_TREE)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_source_kinds"
    ]
    assert calls, "no call site found — the accessor is unused"

    membership_operands = {
        id(comparator)
        for node in ast.walk(_MODULE_TREE)
        if isinstance(node, ast.Compare)
        and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)
        for comparator in node.comparators
    }
    for call in calls:
        assert id(call) in membership_operands, (
            "a use site consumes the vocabulary as something other than a membership "
            "test; the tuple -> frozenset conversion would lose information"
        )


# ── AC4: an unknown label is still rejected ───────────────────────────────────


def test_unknown_provenance_label_is_rejected():
    with pytest.raises(ValueError, match=re.escape("unknown source kind at core.k: 'plugin'")):
        _snapshot({"core": {"k": "plugin"}})


@pytest.mark.parametrize("label", sorted(rebar_config.LAYER_ORDER))
def test_every_known_layer_label_is_accepted(label):
    assert _snapshot({"core": {"k": label}}).sources["core"]["k"] == label
