"""The late-binding regression test must not leave a DUPLICATE ``ensures`` module behind.

``tests/unit/test_ensures_late_binding.py`` reproduces the d720-fc72 import-time capture
window by evicting ``rebar._store.ensures`` from ``sys.modules`` and importing it fresh.
Loading a submodule also rebinds the parent-package attribute
(``setattr(rebar._store, "ensures", <new module>)``), so restoring ``sys.modules`` alone
leaves TWO live copies of the module in the process: ``sys.modules[…]`` holds the original
while ``rebar._store.ensures`` holds the fresh one.

That divergence silently defeats every later ``monkeypatch.setattr(ensures, …)``, because a
module-level ``from rebar._store import ensures`` (bound at collection time) and a
function-local one executed later resolve to DIFFERENT objects — ``IMPORT_FROM`` prefers
``getattr(package, name)`` and only falls back to ``sys.modules`` on ``AttributeError``.
That is exactly how ``tests/interfaces/store/test_fsck_ensures.py::
test_mcp_startup_sweeps_before_run`` saw ``['run']`` instead of ``['ensures', 'run']`` under
an xdist ordering that put this unit test first (bug de95-1594-1056-436e).

This oracle drives the real teardown path and asserts the one invariant that matters: after
it runs, the module is not duplicated.
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
