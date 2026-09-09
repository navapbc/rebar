"""Keep the apply oracle on the runtime module used by ``reconcile`` (bug ae96-72a9-8145-4c85).

Non-editable CI can expose checkout and installed engine copies. ``_loader.lazy_load`` caches
the canonical ``sys.modules`` key, while a by-path reload can patch the unused copy and leave
apply with ``client=None``. This test manufactures that split in editable installs and
reproduces the transport assertion.
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
    """Restore every ``sys.modules`` mutation so this cache regression cannot leak."""
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
    """Forward transport when canonical engine modules came from another copy."""
    other_install = tmp_path / "site-packages" / "rebar" / "_engine"
    other_install.parent.mkdir(parents=True)
    # Skip __pycache__: it is ~3 MB of bytecode this test never executes.
    shutil.copytree(_CHECKOUT_ENGINE, other_install, ignore=shutil.ignore_patterns("__pycache__"))
    other_pkg = other_install / "rebar_reconciler"
    assert other_pkg.joinpath("runtime.py").is_file()

    # Seed canonical keys from the alternate copy, matching an earlier non-editable test.
    for key in _reconciler_keys():
        del sys.modules[key]
    _load_by_path("rebar_reconciler._loader", other_pkg / "_loader.py")
    _load_by_path("rebar_reconciler.runtime", other_pkg / "runtime.py")

    oracle = _load_by_path("_ae96_probe_apply_bindings_oracle", _ORACLE)

    with pytest.MonkeyPatch.context() as inner:
        oracle.test_reconcile_once_threads_composed_runtime_transport_into_apply(
            inner, tmp_path / "pass"
        )
