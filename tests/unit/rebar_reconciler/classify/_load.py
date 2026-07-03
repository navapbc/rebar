"""Shared loader for the pure classifier module under test.

Follows the importlib loader convention established across the reconciler test
tree (see the state/ tests and tests/unit/rebar_reconciler/conftest.py): the
module under test is named at the call site so a move/rename surfaces a clear
loader error rather than a confusing ImportError.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SRC = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "rebar"
    / "_engine"
    / "rebar_reconciler"
    / "classify.py"
)


def load_classify():
    # A FLAT, unique key — NOT "rebar_reconciler.classify": this test dir is itself
    # the importable package ``rebar_reconciler.classify`` under pytest, so caching
    # the engine module under that dotted key would shadow the test package
    # ("not a package" collection error). classify.py is stdlib-only with no
    # internal dotted imports, so a flat name loads cleanly.
    spec = importlib.util.spec_from_file_location("_classify_under_test", _SRC)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod
