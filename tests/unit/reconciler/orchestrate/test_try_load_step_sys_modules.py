"""Test that defect #4 (NoneType.__dict__ from _try_load_step not registering in sys.modules)
doesn't recur.
"""

import importlib
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
GUARDED_PASS_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "guarded_pass.py"


def test_try_load_step_registers_module_in_sys_modules():
    """Register the dotted module in sys.modules before exec_module so dataclass type
    resolution can find its namespace.
    """
    guarded_mod = importlib.import_module("rebar.reconciler.guarded_pass")

    applier = guarded_mod._try_load_step("applier")
    # Module must be loaded successfully
    assert applier is not None, "_try_load_step('applier') must return the loaded module"
    # Module must be registered in sys.modules under its dotted name
    assert "rebar.reconciler.applier" in sys.modules, (
        "_try_load_step must register module under 'rebar.reconciler.applier' "
        "in sys.modules so that dataclass type-resolution works on Python 3.14"
    )
    assert sys.modules["rebar.reconciler.applier"] is applier, (
        "sys.modules['rebar.reconciler.applier'] must be the same module object "
        "_try_load_step returned"
    )
    # ApplyResult dataclass should be instantiable without dataclasses crashing
    if hasattr(applier, "ApplyResult"):
        # Just access __dataclass_fields__ — that triggers type resolution
        assert applier.ApplyResult.__dataclass_fields__ is not None
