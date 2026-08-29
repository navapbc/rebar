"""Bug 79a4 — the ``run_differs`` WIRING of ``refresh_scoped_snapshot`` must be
covered offline.

The overlay ``refresh_scoped_snapshot`` (bug f449) has genuine behaviour teeth: 13
tests in ``test_scoped_snapshot_overlay_f449.py`` pin its field-merge, enrichment
preservation, transport/404 fallback, and the stale-snapshot clobber pair — but they
all call the overlay **directly**. NOTHING asserted that ``run_differs`` actually
invokes it. Measured while filing ticket 79a4-eae7-e5c7-4713 (and re-proven here):
commenting out the
``refresh_scoped_snapshot(ctx)`` call at ``run_differs.py:237`` leaves the entire
offline reconciler suite (3,636 tests) GREEN. Only the live Jira DC lane caught the
drop. A refactor / bad merge / the ongoing module-size splits could silently remove the
call and reintroduce f449's clobber.

This test closes the wiring gap. It asserts the CALL HAPPENED (spy) with the RIGHT
``ctx`` — not that the downstream snapshot looks fresh (a coincidentally-fresh fixture
would satisfy that vacuously; ticket AC2). It goes RED when the call is deleted and
GREEN when present — the mutation-survivor kill.

Rename / module-move robustness (ticket AC + Testing note): ``run_differs`` imports the
overlay LOCALLY (``from rebar_reconciler.snapshot_lagfree_refresh import
refresh_scoped_snapshot``), so the spy is installed on the **source module**
``snapshot_lagfree_refresh`` rather than on ``run_differs``' namespace. That target
survives ``run_differs`` being relocated to another module by a split — exactly the drop
scenario the ticket calls out — because the call always re-resolves the name from
``snapshot_lagfree_refresh`` at execution time.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
RECON_DIR = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"

if "rebar_reconciler" not in sys.modules:  # pragma: no cover - import bootstrap
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
    return _load("run_differs_wiring_79a4_test", "run_differs.py")


@pytest.fixture
def snapshot_lagfree_mod():
    # The exact module object ``run_differs``'s local import resolves at call time.
    return importlib.import_module("rebar_reconciler.snapshot_lagfree_refresh")


class _SpyRefresh:
    """Records every invocation of ``refresh_scoped_snapshot`` (arg + count)."""

    def __init__(self) -> None:
        self.calls: list[object] = []

    def __call__(self, ctx: object) -> None:
        self.calls.append(ctx)


def _stub_surrounding_phases(run_differs_mod, monkeypatch):
    """Neutralise every phase around the overlay wiring so ``run_differs`` runs to
    completion over an empty pass and the only collaborator we observe is the spy."""
    monkeypatch.setattr(run_differs_mod, "_run_differs_invariants", lambda ctx: (False, set(), []))
    for name in (
        "_run_differs_report_schema_drift",
        "_run_differs_inbound",
        "_run_differs_binding_walk",
    ):
        monkeypatch.setattr(run_differs_mod, name, lambda *a, **k: None)
    monkeypatch.setattr(run_differs_mod, "_load_reconcile_backend", lambda: None)
    monkeypatch.setattr(run_differs_mod, "_run_differs_outbound", lambda *a, **k: ([], {}, None))


def _empty_ctx():
    """A minimal ``_PassContext``-shaped ctx that drives the real ``run_differs``
    orchestrator over an empty pass (every other field is read via ``getattr`` with a
    default, so a partial namespace is tolerated)."""
    return types.SimpleNamespace(
        differ=types.SimpleNamespace(compute_mutations=lambda *a, **k: []),
        invariants_mod=None,
        prev_snapshot={},
        curr_snapshot={},
        mutations=[],
    )


def test_run_differs_invokes_refresh_scoped_snapshot_once_with_ctx(
    run_differs_mod, snapshot_lagfree_mod, monkeypatch
):
    """The wiring contract: driving the real ``run_differs(ctx)`` invokes
    ``refresh_scoped_snapshot`` exactly once, with the SAME ``ctx``. Deleting the call
    at ``run_differs.py:237`` makes ``spy.calls`` empty -> RED (the mutation kill)."""
    spy = _SpyRefresh()
    monkeypatch.setattr(snapshot_lagfree_mod, "refresh_scoped_snapshot", spy)
    _stub_surrounding_phases(run_differs_mod, monkeypatch)

    ctx = _empty_ctx()
    run_differs_mod.run_differs(ctx)

    # Assert the INVOCATION itself (AC2): the call happened, exactly once...
    assert len(spy.calls) == 1, (
        "run_differs must invoke refresh_scoped_snapshot exactly once; "
        f"observed {len(spy.calls)} call(s). If 0, the f449 overlay wiring was dropped."
    )
    # ...and with the RIGHT ctx (identity, not a coincidental look-alike).
    assert spy.calls[0] is ctx


def test_refresh_scoped_snapshot_runs_before_the_snapshot_differ(
    run_differs_mod, snapshot_lagfree_mod, monkeypatch
):
    """Ordering half of the contract (f449): the overlay must refresh ``curr_snapshot``
    BEFORE the differ reads it, otherwise the differ arbitrates on the stale snapshot.
    We record the observed order of the two collaborators and assert the overlay ran
    first. This also kills a mutant that moves the call to AFTER ``compute_mutations``."""
    order: list[str] = []

    def _spy_refresh(ctx: object) -> None:
        order.append("refresh")

    def _spy_compute(*_a, **_k):
        order.append("differ")
        return []

    monkeypatch.setattr(snapshot_lagfree_mod, "refresh_scoped_snapshot", _spy_refresh)
    _stub_surrounding_phases(run_differs_mod, monkeypatch)

    ctx = _empty_ctx()
    ctx.differ = types.SimpleNamespace(compute_mutations=_spy_compute)
    run_differs_mod.run_differs(ctx)

    assert order[:2] == ["refresh", "differ"], (
        "refresh_scoped_snapshot must run before differ.compute_mutations; "
        f"observed order was {order}"
    )
