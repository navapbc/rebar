"""Held-out oracle for ADR-0111 reconciler internal-shim removal."""

from __future__ import annotations

from types import SimpleNamespace


def test_old_private_reconciler_shim_bindings_are_removed() -> None:
    import rebar_reconciler.apply_inbound_records as records
    import rebar_reconciler.mode as mode
    import rebar_reconciler.reconcile as reconcile

    assert not hasattr(records, "_inbound_update_write_edit_event")
    for name in (
        "StatusMappingError",
        "preflight_status_mapping",
        "_commit_binding_store_snapshot",
        "_read_local_tickets",
        "SelectionStaleError",
        "ensure_selection_current",
        "narrow_selection_inputs",
        "_build_filter_target_set",
        "_mutation_matches_filter",
        "_build_plan_entries",
        "_NoOpSyncLogger",
        "_write_prev_snapshot_key_set",
        "_accepts_synced_fields_out",
        "_accepts_client",
        "_accepts_ticket_plans",
        "_advance_baselines",
        "_advance_peer_parent",
        "_write_facade_enabled",
        "_resolve_pass_transport",
        "bind_operation_runtime",
        "compose_reconciler_runtime",
    ):
        assert not hasattr(reconcile, name), f"reconcile.{name} is still a private shim"
    assert not hasattr(mode.Mode.DRY_RUN, "rank")


def test_mode_ordering_remains_observable_without_rank() -> None:
    from rebar_reconciler.mode import Mode

    ordered = [
        Mode.DRY_RUN,
        Mode.BOOTSTRAP_STRICT,
        Mode.BOOTSTRAP_THROTTLE,
        Mode.LIVE,
    ]
    assert sorted(reversed(ordered)) == ordered
    assert Mode.DRY_RUN < Mode.BOOTSTRAP_STRICT < Mode.BOOTSTRAP_THROTTLE < Mode.LIVE


def test_reconcile_once_uses_canonical_runtime_composer(monkeypatch, tmp_path) -> None:
    import rebar_reconciler.reconcile as reconcile
    import rebar_reconciler.runtime as runtime

    captured_transport = SimpleNamespace(name="canonical-transport")

    class _FakeRuntime:
        settings = SimpleNamespace(project="REB", backend_name="jira")

        def build_backend(self, transport=None):
            return SimpleNamespace(transport=captured_transport, project="REB")

    monkeypatch.setattr(runtime, "compose_reconciler_runtime", lambda **kw: _FakeRuntime())

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

    original_load = reconcile._load

    def _stub_load(name, relpath):
        if name == "reconcile_run_differs":
            return SimpleNamespace(run_differs=lambda ctx: None)
        return original_load(name, relpath)

    monkeypatch.setattr(reconcile, "_load", _stub_load)
    monkeypatch.setattr(reconcile, "_load_snapshots", _noop_load_snapshots)
    monkeypatch.setattr(reconcile, "_persist_and_log", lambda ctx: {})

    reconcile.reconcile_once("shim-removal", repo_root=tmp_path)

    assert recorded["client"] is captured_transport
