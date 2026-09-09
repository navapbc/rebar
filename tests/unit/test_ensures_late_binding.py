"""Require the ensure registry to late-bind ``lock.canonical_tracker``.

The write path can first import ``ensures`` while that function is monkeypatched.
This test recreates the import window, removes the patch, and proves subsequent
operations use the current function rather than a captured test lambda.
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
    """Temporarily evict ``name``, then restore its module and parent-package bindings."""
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
