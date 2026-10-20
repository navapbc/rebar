"""Story a118 census expand-contract: the binding walk emits the canonical
`decision_census` event (classify.py:351) and NO LONGER emits the old
`binding_walk_census` (which had no consumers — a direct rename with a one-line
rollback comment at run_differs.py).
"""

from __future__ import annotations

import importlib
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RECON_DIR = REPO_ROOT / "src" / "rebar" / "reconciler"


@pytest.fixture(scope="module")
def run_differs_mod():
    return importlib.import_module("rebar.reconciler.run_differs")


@pytest.fixture(scope="module")
def binding_store_mod():
    return importlib.import_module("rebar.reconciler.binding_store")


@pytest.fixture(scope="module")
def outbound_differ_mod():
    return importlib.import_module("rebar.reconciler.outbound_differ")


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
