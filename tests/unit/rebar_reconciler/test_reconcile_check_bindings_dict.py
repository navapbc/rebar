"""Regression test for Bug 0776: reconcile-check stub returned list instead of dict.

Bug summary:
  `__main__._run_reconcile_check` checked `hasattr(applier, 'BindingStore')`
  to decide whether to use the real binding store or a stub. But BindingStore
  lives in binding_store.py, not applier.py — so the hasattr check ALWAYS
  failed, and the stub `_EmptyBindings.all_bindings()` returned a list.
  reconcile_check.reconcile_check() then crashed with
  `'list' object has no attribute 'items'`.

Fix:
  Load binding_store.py directly via `load_binding_store(repo_root)` — the
  same factory reconcile.py uses. The stub fallback now returns a dict.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MAIN_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "__main__.py"


def _load_main():
    spec = importlib.util.spec_from_file_location("main_under_test", MAIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["main_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_reconcile_check_passes_dict_binding_store(tmp_path, monkeypatch):
    """_run_reconcile_check must hand reconcile_check a binding store whose
    all_bindings() returns a dict (not a list). Regression for Bug 0776."""
    main_mod = _load_main()

    captured: dict = {}

    class _FakeRcMod:
        @staticmethod
        def reconcile_check(local_tickets, jira_snapshot, binding_store):
            captured["binding_store"] = binding_store
            captured["all_bindings_result"] = binding_store.all_bindings()
            return {
                "total_bindings": 0,
                "checked": 0,
                "in_sync": 0,
                "discrepancies": [],
                "orphaned_bindings": [],
                "orphaned_jira": [],
                "unbound_local": 0,
                "unbound_jira": 0,
            }

        @staticmethod
        def format_report(report):
            return "fake report"

        @staticmethod
        def write_report_json(report, path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}")

    class _FakeFetcher:
        @staticmethod
        def compute_snapshot(pass_id, repo_root):
            # No-write counterpart: reconcile-check must not write a snapshot.
            return {}

    def _fake_try_load_step(name):
        if name == "reconcile_check":
            return _FakeRcMod
        if name == "fetcher":
            return _FakeFetcher
        if name == "binding_store":
            # Return None to force the fallback stub path — the regression
            # is that the FALLBACK returns dict, not list.
            return None
        return None

    monkeypatch.setattr(main_mod, "_try_load_step", _fake_try_load_step)

    rc = main_mod._run_reconcile_check(tmp_path)
    assert rc == 0, f"_run_reconcile_check returned {rc}"

    # Critical regression assertion: the binding store passed to reconcile_check
    # must have all_bindings() returning a dict (.items() must work).
    binding_store = captured["binding_store"]
    bindings = binding_store.all_bindings()
    assert isinstance(bindings, dict), (
        f"all_bindings() returned {type(bindings).__name__}, expected dict. "
        f"reconcile_check calls .items() on this and will crash on a list."
    )
    # Sanity-check the .items() call works (the actual crash mode).
    list(bindings.items())


def test_reconcile_check_uses_binding_store_module_when_available(tmp_path, monkeypatch):
    """When binding_store.py is loadable, _run_reconcile_check must use its
    load_binding_store factory — not the stub. Verifies the fix routes
    through the real BindingStore path."""
    main_mod = _load_main()

    captured: dict = {}

    class _FakeBindingStore:
        def all_bindings(self) -> dict:
            return {"local-1": {"jira_key": "DIG-1", "state": "confirmed"}}

    class _FakeBindingStoreMod:
        @staticmethod
        def load_binding_store(repo_root):
            captured["repo_root"] = repo_root
            return _FakeBindingStore()

    class _FakeRcMod:
        @staticmethod
        def reconcile_check(local_tickets, jira_snapshot, binding_store):
            captured["binding_store_class"] = type(binding_store).__name__
            return {
                "total_bindings": 1, "checked": 0, "in_sync": 0,
                "discrepancies": [], "orphaned_bindings": [],
                "orphaned_jira": [], "unbound_local": 0, "unbound_jira": 0,
            }

        @staticmethod
        def format_report(r):
            return ""

        @staticmethod
        def write_report_json(r, p):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{}")

    class _FakeFetcher:
        @staticmethod
        def compute_snapshot(pass_id, repo_root):
            # No-write counterpart: reconcile-check must not write a snapshot.
            return {}

    def _fake_try_load_step(name):
        if name == "reconcile_check":
            return _FakeRcMod
        if name == "fetcher":
            return _FakeFetcher
        if name == "binding_store":
            return _FakeBindingStoreMod
        return None

    monkeypatch.setattr(main_mod, "_try_load_step", _fake_try_load_step)

    rc = main_mod._run_reconcile_check(tmp_path)
    assert rc == 0
    assert captured["binding_store_class"] == "_FakeBindingStore", (
        f"expected the binding_store_mod-loaded BindingStore to be used, "
        f"got: {captured.get('binding_store_class')}"
    )
    assert captured["repo_root"] == tmp_path
