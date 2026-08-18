"""RP-04 S3 (6e3b) HAPPY-path oracle — reconciler apply uses the composed runtime.

Declared AC1-AC6 gate file (see ticket verify_commands). The implementer works
against THIS happy path; the poisoned-ambient / child-env-sibling / concurrency edge
cases live in a held-out oracle the implementer does not see.

Contract: ``reconcile.reconcile_once`` composes ONE ``ReconcilerRuntime`` (S2's
``compose_reconciler_runtime``) per pass and threads its already-built backend's
transport into the apply phase, forwarding it to ``applier.apply`` as ``client=`` —
rather than each apply re-resolving ambient config via ``_load_acli``. Asserts the
recorded ``client`` argument (observable behavior), never private source text.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[3]
ENGINE = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(name: str, rel: str):
    path = ENGINE / rel
    cached = sys.modules.get(name)
    if cached is not None and getattr(cached, "__file__", None) == str(path):
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_reconcile_once_threads_composed_runtime_transport_into_apply(monkeypatch, tmp_path):
    """A pass composes one runtime and the apply phase receives its captured transport."""
    reconcile = _load("rebar_reconciler.reconcile", "reconcile.py")

    captured_transport = SimpleNamespace(name="composed-transport")

    class _FakeRuntime:
        settings = SimpleNamespace(project="REB", backend_name="jira")

        def build_backend(self, transport=None):
            return SimpleNamespace(transport=captured_transport, project="REB")

    monkeypatch.setattr(
        reconcile, "compose_reconciler_runtime", lambda **kw: _FakeRuntime(), raising=False
    )

    recorded = {}

    def _record_apply(mutations, pass_id=None, repo_root=None, *, client=None, **kw):
        recorded["client"] = client
        return tmp_path / "manifest.json"

    def _noop_load_snapshots(ctx):
        ctx.mutations = []
        ctx.binding_store = None
        ctx.sync_logger = SimpleNamespace(log=lambda *a, **k: None)
        ctx.applier = SimpleNamespace(apply=_record_apply)
        ctx.persist = False

    # run_differs is loaded dynamically via reconcile._load; stub that one key.
    _orig_load = reconcile._load

    def _stub_load(name, relpath):
        if name == "reconcile_run_differs":
            return SimpleNamespace(run_differs=lambda ctx: None)
        return _orig_load(name, relpath)

    monkeypatch.setattr(reconcile, "_load", _stub_load, raising=True)
    monkeypatch.setattr(reconcile, "_load_snapshots", _noop_load_snapshots, raising=True)
    monkeypatch.setattr(reconcile, "_persist_and_log", lambda ctx: {}, raising=True)

    reconcile.reconcile_once("pass-happy", repo_root=tmp_path)

    assert recorded.get("client") is captured_transport, (
        "the apply phase must forward the composed runtime's transport as client="
    )
