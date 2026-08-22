"""The ensure registry must late-bind ``lock.canonical_tracker`` (bug d720-fc72).

``rebar._store.ensures`` is imported LAZILY by the write path
(``event_append.write_and_push`` → ``maybe_emit_pending_hint``), so its first import
can execute while a test holds ``lock.canonical_tracker`` monkeypatched — exactly what
``test_push_callers_best_effort.py`` does. A by-value ``from rebar._store.lock import
canonical_tracker`` executed in that window captures the TEST'S lambda permanently:
monkeypatch teardown restores ``lock.canonical_tracker`` but not the copy already bound
inside ``ensures``. Every later ``run_ensures``/``applied_ids`` in the process then
canonicalizes EVERY tracker to the polluter's dead tmpdir, so the init-time ensure
sweep (including the ``gc-config`` autoDetach pins) lands in the wrong directory while
reporting ``changed`` — which is how a later test's freshly-inited tracker was left
unpinned and a push left detached ``git maintenance`` behind
(``test_a_write_pushing_to_the_origin_leaves_no_detached_upkeep``).

This reproduces that capture window deterministically: import ``ensures`` fresh while
``lock.canonical_tracker`` is patched to a decoy, drop the patch, and assert the module
resolves trackers through the CURRENT ``lock.canonical_tracker`` — not a captured copy.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MODULE = "rebar._store.ensures"


@contextmanager
def _reimportable(name: str) -> Iterator[None]:
    """Drop ``name`` so the body can import it fresh, then FULLY restore it.

    Restoring ``sys.modules`` alone is not enough, and that shortfall was bug
    de95-1594-1056-436e. Loading a submodule also rebinds the attribute on its
    parent package (the import system does ``setattr(rebar._store, "ensures",
    <fresh module>)``), so a ``sys.modules``-only restore leaves TWO live copies
    of the same file: the original in ``sys.modules`` and the fresh one on the
    package. ``from rebar._store import ensures`` resolves through
    ``getattr(package, "ensures")`` — CPython's IMPORT_FROM only falls back to
    ``sys.modules`` on AttributeError — so a later test that monkeypatches the
    ``sys.modules`` copy no longer affects what production code (e.g. the
    function-local import in ``rebar.mcp_server.main``) actually gets. That
    leak made ``tests/interfaces/store/test_fsck_ensures.py::
    test_mcp_startup_sweeps_before_run`` flake whenever this module ran earlier
    in the same pytest-xdist worker. Restore BOTH bindings, always.
    """
    parent_name, _, attr = name.rpartition(".")
    parent = sys.modules.get(parent_name)
    original = sys.modules.pop(name, None)
    try:
        yield
    finally:
        sys.modules.pop(name, None)
        restored = original if original is not None else importlib.import_module(name)
        sys.modules[name] = restored
        if parent is not None:
            setattr(parent, attr, restored)


def test_ensures_imported_under_a_canonical_tracker_patch_does_not_capture_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._store import lock

    real = tmp_path / "real-tracker"
    real.mkdir()
    (real / ".ensure-applied").write_text('["gc-config"]', encoding="utf-8")
    decoy = tmp_path / "decoy-tracker"
    decoy.mkdir()

    with _reimportable(_MODULE):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(lock, "canonical_tracker", lambda _tracker: str(decoy))
            ensures = importlib.import_module(_MODULE)
        # The patch is gone; a captured copy would still resolve to the decoy and
        # read its (absent) marker as the empty set.
        assert ensures.applied_ids(real) == {"gc-config"}, (
            "ensures resolved the tracker through a canonical_tracker captured at "
            "import time; it must late-bind lock.canonical_tracker"
        )
