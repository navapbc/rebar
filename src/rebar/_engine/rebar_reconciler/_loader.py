"""Shared by-path sibling loader for the rebar_reconciler package.

The reconciler package is routinely loaded **by file path** rather than by
ordinary import: the top-level ``rebar_reconciler`` name is shadowed by a
same-named test package, and several modules are exec'd standalone via
``importlib.util.spec_from_file_location`` (no package context, so
``from rebar_reconciler.x import ...`` cannot resolve). To load a sibling in
those contexts, ~two dozen ``_load_*`` helpers across the package hand-rolled
the identical ``spec_from_file_location`` + ``sys.modules`` cache + ``exec``
dance. This module collapses that idiom into one function.

**The sys.modules key contract is load-bearing.** Callers pass an *exact* key
string (e.g. ``"rebar_reconciler.mutation"``, ``"reconcile_applier"``,
``"rebar_reconciler_errors"``); tests pre-seed those exact keys to inject stubs
and to preserve class identity (``Mutation`` / enum members) across modules.
``lazy_load`` therefore:

* returns the already-registered module when ``key`` is present in
  ``sys.modules`` (so a pre-seeded stub wins), and
* registers the freshly created module under ``key`` **before** executing it
  (so ``@dataclass`` bodies and any circular sibling load during exec see the
  module already in ``sys.modules`` — the ordering fixed in bug 5be7 defect #4).

The file to load is resolved relative to *this* module's directory, which is the
package directory shared by every sibling — identical to each caller's historic
``Path(__file__).parent / filename``.

This module imports **only stdlib** and nothing from ``rebar_reconciler`` so it
stays loadable both as a normal package submodule and standalone by file path.
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
