"""Ensure the late-binding regression test restores one ``ensures`` module.

Fresh submodule imports replace both ``sys.modules`` and the parent package's
attribute. Teardown must restore both bindings so later imports and monkeypatches
resolve the same object regardless of xdist ordering.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MODULE = "rebar._store.ensures"


def test_late_binding_test_leaves_no_duplicate_ensures_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_ensures_late_binding as late_binding  # same-directory helper (bare name)

    import rebar._store as store_pkg

    importlib.import_module(_MODULE)  # the state any consumer of ensures starts from

    # Precondition: the process holds ONE ensures module, reachable both ways.
    assert sys.modules[_MODULE] is store_pkg.ensures, (
        "precondition violated: rebar._store.ensures already diverges from "
        "sys.modules — some earlier test leaked a duplicate module"
    )

    late_binding.test_ensures_imported_under_a_canonical_tracker_patch_does_not_capture_it(
        tmp_path, monkeypatch
    )

    assert sys.modules[_MODULE] is store_pkg.ensures, (
        "the late-binding test restored sys.modules but not the parent-package "
        "attribute, leaving a duplicate rebar._store.ensures; a later "
        "`from rebar._store import ensures` gets the un-monkeypatchable copy"
    )
