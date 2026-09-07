"""Load reconciler siblings by path under caller-supplied canonical module keys.

A pre-registered ``sys.modules`` entry wins. New modules are registered before
execution so decorators and circular sibling loads resolve the same object.
Paths are relative to this package directory. The module uses only the standard
library and supports normal or standalone loading.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_PACKAGE_DIR = Path(__file__).parent


def lazy_load(key: str, filename: str) -> ModuleType:
    """Load sibling ``filename`` under sys.modules ``key`` (cache-returning).

    If ``key`` is already registered in ``sys.modules`` the cached module is
    returned unchanged (this is what lets test fixtures pre-seed a patched
    module and have production code reuse it). Otherwise the sibling file is
    loaded via ``spec_from_file_location``, registered under ``key`` **before**
    ``exec_module`` runs, and returned.

    ``key`` and ``filename`` are passed through verbatim — the caller owns the
    exact key string, which is part of the package's load-bearing key contract.
    """
    if key in sys.modules:
        return sys.modules[key]
    path = _PACKAGE_DIR / filename
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create module spec for {key!r} at {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod
