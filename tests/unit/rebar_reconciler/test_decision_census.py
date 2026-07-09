"""Story a118 census expand-contract: the binding walk emits the canonical
`decision_census` event (classify.py:351) and NO LONGER emits the old
`binding_walk_census` (which had no consumers — a direct rename with a one-line
rollback comment at run_differs.py).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RECON_DIR = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"

if "rebar_reconciler" not in sys.modules:
    _pkg = types.ModuleType("rebar_reconciler")
    _pkg.__path__ = [str(RECON_DIR)]
    sys.modules["rebar_reconciler"] = _pkg


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, RECON_DIR / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def run_differs_mod():
    return _load("run_differs_census_test", "run_differs.py")


@pytest.fixture(scope="module")
def binding_store_mod():
    return _load("binding_store_census_test", "binding_store.py")


@pytest.fixture(scope="module")
def outbound_differ_mod():
    return _load("outbound_differ_census_test", "outbound_differ.py")


class _RecordingLogger:
    def __init__(self):
        self.events: list[str] = []

    def log(self, name, **kw):
        self.events.append(name)


def test_binding_walk_emits_decision_census_not_binding_walk_census(
    run_differs_mod, binding_store_mod, outbound_differ_mod, tmp_path
):
    """An empty-store binding walk still emits the census — under the canonical
    `decision_census` name, and never the retired `binding_walk_census`."""
    logger = _RecordingLogger()
    ctx = types.SimpleNamespace(
        repo_root=tmp_path,
        persist=False,
        local_tickets=[],
        binding_store=binding_store_mod.BindingStore(tmp_path / ".tickets-tracker"),
        curr_snapshot={},
        outbound_differ_mod=outbound_differ_mod,
        sync_logger=logger,
    )

    run_differs_mod._run_differs_binding_walk(ctx, [], None)

    assert "decision_census" in logger.events, "the canonical decision_census must be emitted"
    assert "binding_walk_census" not in logger.events, "the retired event name must be gone"
