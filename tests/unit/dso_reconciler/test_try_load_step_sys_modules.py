"""Test that defect #4 (NoneType.__dict__ from _try_load_step not registering in sys.modules) doesn't recur."""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MAIN_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "__main__.py"


def test_try_load_step_registers_module_in_sys_modules():
    """_try_load_step must register the loaded module in sys.modules under its
    dotted spec name BEFORE exec_module, so that dataclass type-resolution
    (which does sys.modules.get(cls.__module__).__dict__) does not fail with
    AttributeError: 'NoneType' object has no attribute '__dict__' on Python 3.14.

    Regression: bug 5be7 chain defect #4 — the original _try_load_step loaded
    via importlib.util.spec_from_file_location + exec_module but never wrote
    to sys.modules. ApplyResult (a frozen dataclass in applier.py) then had
    __module__='dso_reconciler.applier' but sys.modules.get('dso_reconciler.applier')
    returned None, causing dataclasses._is_type to crash.
    """
    # Load __main__ fresh
    spec = importlib.util.spec_from_file_location(
        "_try_load_step_test_main", MAIN_PATH
    )
    main_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main_mod)

    # Snapshot sys.modules to detect what _try_load_step registers
    pre_keys = set(sys.modules.keys())
    try:
        applier = main_mod._try_load_step("applier")
        # Module must be loaded successfully
        assert applier is not None, (
            "_try_load_step('applier') must return the loaded module"
        )
        # Module must be registered in sys.modules under its dotted name
        assert "dso_reconciler.applier" in sys.modules, (
            "_try_load_step must register module under 'dso_reconciler.applier' "
            "in sys.modules so that dataclass type-resolution works on Python 3.14"
        )
        assert sys.modules["dso_reconciler.applier"] is applier, (
            "sys.modules['dso_reconciler.applier'] must be the same module object "
            "_try_load_step returned"
        )
        # ApplyResult dataclass should be instantiable without dataclasses crashing
        if hasattr(applier, "ApplyResult"):
            # Just access __dataclass_fields__ — that triggers type resolution
            assert applier.ApplyResult.__dataclass_fields__ is not None
    finally:
        # Cleanup: remove anything _try_load_step added
        for k in list(sys.modules.keys()):
            if k not in pre_keys and "dso_reconciler" in k:
                sys.modules.pop(k, None)
