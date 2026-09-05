"""Bug ae96-72a9-8145-4c85 — the apply-bindings oracle must patch the runtime module IN USE.

WHAT WENT WRONG. Under a NON-editable install (``uv pip install '.[…]'`` — what
``sweep (py3.15)``, ``sweep (py3.14t)``, ``sweep (macos, full suite)`` and ``sweep (windows)``
do, unlike the gate's editable ``uv sync --locked``) the reconciler engine exists TWICE on
disk: in the checkout at ``src/rebar/_engine/rebar_reconciler/`` and in site-packages. Which
copy wins a canonical ``rebar_reconciler.*`` ``sys.modules`` key depends on which test got
there first — ``tests/unit/conftest.py``'s shadow bridge resolves the CHECKOUT, while
``rebar._engine.engine_dir()`` (used by e.g. ``test_reconciler_operation_bindings.py``)
resolves the INSTALLED copy.

``reconcile.py`` reaches its siblings through ``_loader.lazy_load``, which caches by KEY and
deliberately ignores the path, so ``reconcile._runtime`` is whichever copy was already
registered. The oracle used to load ``rebar_reconciler.runtime`` a SECOND time through its own
by-path helper; that helper's ``__file__`` guard (bug 9f0b, pinned by
``diffing/test_load_module_identity.py`` and NOT the defect) then read the live module as a
MISS, replaced the shared key with a checkout-loaded copy, and the oracle monkeypatched the
copy nobody used. The real ``compose_reconciler_runtime`` stayed in play, raised for a
scope-less ``tmp_path``, ``bind_operation_runtime`` swallowed it for a non-persisting pass,
and apply was handed ``client=None``: ``assert None is namespace(name='composed-transport')``.

WHY THIS TEST HAS TEETH ANYWHERE. A dev checkout is installed EDITABLE, so the two copies are
one directory and the split cannot occur — which is why the failure only ever appeared on CI.
This test manufactures the split in ANY install mode: it copies the engine to a stand-in
"other install" location and seeds the canonical keys from it exactly as an earlier test
would, then runs the oracle. It reproduces the CI assertion verbatim against any oracle that
resolves the runtime module by path instead of taking the one ``reconcile`` resolved.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_ORACLE = Path(__file__).resolve().parent / "test_reconciler_apply_bindings.py"
_CHECKOUT_ENGINE = Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine"


def _reconciler_keys() -> list[str]:
    return [k for k in sys.modules if k == "rebar_reconciler" or k.startswith("rebar_reconciler.")]


@pytest.fixture
def restore_sys_modules() -> Iterator[None]:
    """Undo every ``sys.modules`` change this test makes.

    Mandatory, not hygiene: the subject IS a module-cache leak across test files, so a test
    that proved the invariant while leaking would be causing the bug it documents.
    """
    before = dict(sys.modules)
    try:
        yield
    finally:
        for name in set(sys.modules) - set(before):
            del sys.modules[name]
        sys.modules.update(before)


def _load_by_path(key: str, path: Path):
    spec = importlib.util.spec_from_file_location(key, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


def test_apply_bindings_oracle_survives_a_split_engine_install(
    restore_sys_modules: None, tmp_path: Path
) -> None:
    """The oracle must still drive apply when the registered engine is not its own copy."""
    other_install = tmp_path / "site-packages" / "rebar" / "_engine"
    other_install.parent.mkdir(parents=True)
    # Skip __pycache__: it is ~3 MB of bytecode this test never executes.
    shutil.copytree(_CHECKOUT_ENGINE, other_install, ignore=shutil.ignore_patterns("__pycache__"))
    other_pkg = other_install / "rebar_reconciler"
    assert other_pkg.joinpath("runtime.py").is_file()

    # Reproduce the seed an earlier test leaves behind on a non-editable install: the
    # canonical keys already hold modules loaded from the OTHER copy of the same source.
    for key in _reconciler_keys():
        del sys.modules[key]
    _load_by_path("rebar_reconciler._loader", other_pkg / "_loader.py")
    _load_by_path("rebar_reconciler.runtime", other_pkg / "runtime.py")

    oracle = _load_by_path("_ae96_probe_apply_bindings_oracle", _ORACLE)

    with pytest.MonkeyPatch.context() as inner:
        oracle.test_reconcile_once_threads_composed_runtime_transport_into_apply(
            inner, tmp_path / "pass"
        )
