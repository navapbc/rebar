"""beer-datum-bark (e534-5154-2401-40fb), facets 2 and 3.

Facet 2 — FAIL LOUD: after isolating per-mutation failures, a pass that recorded
any real per-mutation error must exit NON-ZERO (not the current exit 0). The
benign 400-comment-fallback is NOT a recorded error (reconcile.py:1082-1085 counts
it as applied), so it stays exit 0 — this is fail-loud boundary "Option C".

Facet 3 — PREFLIGHT NO-ABORT: preflight_status_mapping must not raise a
StatusMappingError that aborts the whole pass when a single update mutation carries
an unmapped status. The offending mutation is recorded as a per-mutation failure
downstream (applier backstop) instead of aborting its siblings.

RED pre-fix:
  - run_pass returns 0 even when result["mutation_failures"] > 0.
  - preflight_status_mapping raises StatusMappingError on the first unmapped status.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
MAIN_PATH = SCRIPTS_DIR / "rebar_reconciler" / "__main__.py"
RECONCILE_PATH = SCRIPTS_DIR / "rebar_reconciler" / "reconcile.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if "rebar_reconciler" not in sys.modules:
    import types as _types

    _dr = _types.ModuleType("rebar_reconciler")
    _dr.__path__ = [str(SCRIPTS_DIR / "rebar_reconciler")]
    sys.modules["rebar_reconciler"] = _dr


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ----------------------------- Facet 2: fail loud -----------------------------


def test_run_pass_exits_nonzero_when_mutations_failed(tmp_path: Path) -> None:
    """A completed pass that recorded real per-mutation failures must fail loud
    (non-zero exit), distinct from the reschedule signal (75)."""
    main_mod = _load("reconciler_main_faillloud", MAIN_PATH)

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

    with patch.object(main_mod, "_try_load_step", side_effect=_fake_loader):
        rc = main_mod.run_pass(repo_root=tmp_path, pass_id="test-faillloud")

    assert rc != 0, "a pass with recorded mutation_failures must exit non-zero (fail loud)"
    assert rc != 75, "mutation failures are not a reschedule; must not use EXIT_RESCHEDULE"


def test_run_pass_exits_zero_on_clean_pass(tmp_path: Path) -> None:
    """Guard the opposite: a pass with zero failures still exits 0 (no false alarms;
    the benign comment-fallback counts as applied, not a failure)."""
    main_mod = _load("reconciler_main_clean", MAIN_PATH)

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

    with patch.object(main_mod, "_try_load_step", side_effect=_fake_loader):
        rc = main_mod.run_pass(repo_root=tmp_path, pass_id="test-clean")

    assert rc == 0, "a clean pass (no failures) must still exit 0"


# ----------------------------- Facet 3: preflight -----------------------------


def test_preflight_does_not_abort_on_unmapped_status() -> None:
    """An unmapped local status on a single update mutation must NOT raise
    StatusMappingError (which aborts the entire pass). Post-fix the offending
    mutation is recorded as a per-mutation failure downstream instead."""
    reconcile_mod = _load("reconcile_preflight", RECONCILE_PATH)

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
        reconcile_mod.preflight_status_mapping(mutations)
    except reconcile_mod.StatusMappingError as exc:
        pytest.fail(f"preflight must not abort the pass on an unmapped status; it raised {exc!r}")
