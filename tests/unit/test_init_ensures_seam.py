"""The init ↔ ensure-registry seam: where the convergence units live, and the
``init._<name>`` access path that must keep resolving.

``src/rebar/_commands/init.py`` owns TWO subsystems that never call each other:
host-repo tracker provisioning (``init_core`` → ``_mount_or_create_branch`` /
``_init_via_symlink`` → ``init_cli``), and the check-then-act **convergence units**
whose only caller is :func:`rebar._store.ensures._registry`. The units live in
:mod:`rebar._commands._init_ensures`.

They stay reachable as ``init._<name>``, because that access path is load-bearing:
``ensures._registry()`` dispatches through it, ``tests/interfaces/store/
test_ensure_drift_matrix.py`` imports ``_GITIGNORE`` through it, and ADR 0051,
``docs/migrations.md``, ``docs/scale-envelope.md`` and ``_store/sync.py`` all cite the
units through it. This test pins that contract so a later edit cannot quietly sever it.
"""

from __future__ import annotations

import pytest

from rebar._commands import _init_ensures, init
from rebar._store import ensures

pytestmark = pytest.mark.unit

# unit id → the attribute name of its implementation
_MOVED_UNITS = {
    "gc-config": "_gc_config_unit",
    "merge-ours": "_merge_ours_unit",
    "gitattributes": "_gitattributes_unit",
    "gitignore": "_gitignore_unit",
    "store-compat": "_store_compat_unit",
}

# The tickets-branch content templates the units are the sole readers of; they moved
# with the units because that ownership is why the units lived in ``init`` at all.
_MOVED_TEMPLATES = ("_GITIGNORE", "_GITATTRIBUTES")


@pytest.mark.parametrize("attr", [*_MOVED_UNITS.values(), *_MOVED_TEMPLATES])
def test_moved_symbol_is_defined_in_the_ensures_module(attr: str) -> None:
    """The implementation lives in ``_init_ensures``, not in ``init``."""
    assert hasattr(_init_ensures, attr), f"{attr} must be defined in _init_ensures"


@pytest.mark.parametrize("attr", [*_MOVED_UNITS.values(), *_MOVED_TEMPLATES])
def test_init_reexports_the_same_object(attr: str) -> None:
    """``init._<name>`` still resolves, to the SAME object — an equal-but-distinct
    copy would silently fork the templates and break registry identity."""
    assert getattr(init, attr) is getattr(_init_ensures, attr)


@pytest.mark.parametrize(("uid", "attr"), sorted(_MOVED_UNITS.items()))
def test_registry_dispatches_to_the_moved_unit(uid: str, attr: str) -> None:
    """The ensure registry — the units' only caller — still binds each id to the
    moved implementation."""
    assert uid in ensures.REGISTRY_IDS
    assert ensures._registry()[uid] is getattr(_init_ensures, attr)


def test_untrack_unit_stays_with_the_batch_size_it_reads() -> None:
    """``_untrack_runtime_markers_unit`` is NOT a content converger (it has no
    template; it repairs a legacy index with ``git rm --cached``) and it reads
    ``_UNTRACK_BATCH`` from its own module globals, which
    ``tests/unit/test_ensures_untrack_markers.py`` patches on ``init``. Splitting the
    two apart would leave that patch silently inert, so they stay together."""
    assert init._untrack_runtime_markers_unit.__module__ == "rebar._commands.init"
    assert init._UNTRACK_BATCH in init._untrack_runtime_markers_unit.__globals__.values()
    assert ensures._registry()["untrack-runtime-markers"] is init._untrack_runtime_markers_unit
