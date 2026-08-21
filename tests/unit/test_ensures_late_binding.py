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
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MODULE = "rebar._store.ensures"


def test_ensures_imported_under_a_canonical_tracker_patch_does_not_capture_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._store import lock

    real = tmp_path / "real-tracker"
    real.mkdir()
    (real / ".ensure-applied").write_text('["gc-config"]', encoding="utf-8")
    decoy = tmp_path / "decoy-tracker"
    decoy.mkdir()

    original = sys.modules.pop(_MODULE, None)
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(lock, "canonical_tracker", lambda _tracker: str(decoy))
            ensures = importlib.import_module(_MODULE)
        # The patch is gone; a captured copy would still resolve to the decoy and
        # read its (absent) marker as the empty set.
        assert ensures.applied_ids(real) == {"gc-config"}, (
            "ensures resolved the tracker through a canonical_tracker captured at "
            "import time; it must late-bind lock.canonical_tracker"
        )
    finally:
        sys.modules.pop(_MODULE, None)
        if original is not None:
            sys.modules[_MODULE] = original
        else:
            importlib.import_module(_MODULE)
