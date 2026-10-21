"""beer-datum-bark (e534-5154-2401-40fb), facets 2 and 3.

Facet 2 — FAIL LOUD: after isolating per-mutation failures, a pass that recorded
any real per-mutation error must exit NON-ZERO (not the current exit 0). The
benign 400-comment-fallback is NOT a recorded error (persist_phase.persist_and_log counts
it as applied), so it stays exit 0 — this is fail-loud boundary "Option C".

Facet 3 — PREFLIGHT NO-ABORT: preflight_status_mapping must not raise a
the former StatusMappingError that aborted the whole pass when a single update mutation carries
an unmapped status. The offending mutation is recorded as a per-mutation failure
downstream (applier backstop) instead of aborting its siblings.

RED pre-fix:
  - the pass exits 0 even when result["mutation_failures"] > 0.
  - pass_support.preflight_status_mapping raises on the first unmapped status.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
PASS_SUPPORT_PATH = SCRIPTS_DIR.parent / "reconciler" / "pass_support.py"


# ----------------------------- Facet 2: fail loud -----------------------------


def test_run_pass_exits_nonzero_when_mutations_failed(tmp_path: Path) -> None:
    """A completed pass that recorded real per-mutation failures must fail loud
    (non-zero exit), distinct from the reschedule signal (75)."""
    guarded_mod = importlib.import_module("rebar.reconciler.guarded_pass")

    class _FakeReschedule(Exception):
        pass

    def _fake_loader(name: str):
        if name == "reconcile":
            fake = ModuleType("fake_reconcile")

            def reconcile_once(pass_id, **kwargs):
                return {
                    "pass_id": pass_id,
                    "mutation_count": 3,
                    "mutations_applied": 2,
                    "mutation_failures": 1,  # a real recorded failure
                    "manifest_path": None,
                }

            fake.reconcile_once = reconcile_once  # type: ignore[attr-defined]
            return fake
        if name == "applier":
            fake = ModuleType("fake_applier")
            fake.RescheduleError = _FakeReschedule  # type: ignore[attr-defined]
            fake.EXIT_RESCHEDULE = 75  # type: ignore[attr-defined]
            return fake
        return None

    with patch.object(guarded_mod, "_try_load_step", side_effect=_fake_loader):
        result = guarded_mod.run_pass_result(repo_root=tmp_path, pass_id="test-faillloud")

    assert result.disposition is guarded_mod._Disposition.OPERATIONAL_FAILURE
    rc = result.disposition.legacy_exit
    assert rc != 0, "a pass with recorded mutation_failures must exit non-zero (fail loud)"
    assert rc != 75, "mutation failures are not a reschedule; must not use EXIT_RESCHEDULE"


def test_run_pass_exits_zero_on_clean_pass(tmp_path: Path) -> None:
    """Guard the opposite: a pass with zero failures still exits 0 (no false alarms;
    the benign comment-fallback counts as applied, not a failure)."""
    guarded_mod = importlib.import_module("rebar.reconciler.guarded_pass")

    def _fake_loader(name: str):
        if name == "reconcile":
            fake = ModuleType("fake_reconcile")

            def reconcile_once(pass_id, **kwargs):
                return {
                    "pass_id": pass_id,
                    "mutation_count": 2,
                    "mutations_applied": 2,
                    "mutation_failures": 0,
                    "manifest_path": None,
                }

            fake.reconcile_once = reconcile_once  # type: ignore[attr-defined]
            return fake
        if name == "applier":
            fake = ModuleType("fake_applier")
            fake.EXIT_RESCHEDULE = 75  # type: ignore[attr-defined]
            return fake
        return None

    with patch.object(guarded_mod, "_try_load_step", side_effect=_fake_loader):
        result = guarded_mod.run_pass_result(repo_root=tmp_path, pass_id="test-clean")

    assert result.disposition is guarded_mod._Disposition.CONVERGED
    rc = result.disposition.legacy_exit
    assert rc == 0, "a clean pass (no failures) must still exit 0"


# ----------------------------- Facet 3: preflight -----------------------------


def test_preflight_does_not_abort_on_unmapped_status() -> None:
    """An unmapped local status on a single update mutation must NOT raise
    StatusMappingError (which aborts the entire pass). Post-fix the offending
    mutation is recorded as a per-mutation failure downstream instead."""
    pass_support_mod = importlib.import_module("rebar.reconciler.pass_support")

    mutations = [
        {
            "direction": "outbound",
            "action": "update",
            "key": "DIG-1",
            "fields": {"summary": "fine"},
        },
        {
            "direction": "outbound",
            "action": "update",
            "key": "DIG-2",
            "fields": {"status": "bogus_unmapped_status"},
        },
    ]

    try:
        pass_support_mod.preflight_status_mapping(mutations)
    except pass_support_mod.StatusMappingError as exc:
        pytest.fail(f"preflight must not abort the pass on an unmapped status; it raised {exc!r}")
