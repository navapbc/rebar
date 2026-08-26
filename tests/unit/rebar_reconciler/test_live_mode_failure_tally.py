"""LIVE mode must still count per-mutation failures (bug c903-42b9-0f17-45cc).

`_apply_batch` writes a manifest carrying every mutation outcome, including
`outcome["error"]` for soft-failed ones. `apply_planning._emit_mode_manifest`'s LIVE
branch then UNLINKS that manifest and returns `("RETURN", None)` — "LIVE: no manifest
file per contract" — so `applier.apply()` returns None. `reconcile._persist_and_log`
tallies with:

    mutations_applied = len(mutations)
    mutation_failures = 0
    ...
    elif manifest_path is not None:   # never true in LIVE
        ... mutation_failures = sum(1 for o in outcomes if o.get("error"))

so in LIVE the tally block never runs: failures stay 0 and every computed mutation is
counted as applied. `__main__.run_pass`'s `if failures > 0: return 1` is therefore dead
code in the only mode production runs.

Three contracts are defeated by this, all in LIVE only:
  * e534-5154-2401-40fb — "isolate + fail loud at the END exits non-zero"
  * 48c8-5375-f883-462d — REBAR_RECONCILER_FAIL_SILENT_NOOP=1 (ON in the live workflow)
    is documented to "count toward mutation_failures and drive a non-zero pass exit"
  * 85a1 — the truthful tally; `applied N of N` is printed while mutations failed

Every existing fail-loud/truthful-tally test stubs `reconcile_once` to RETURN a
`mutation_failures` value, so none of them exercise this path. These tests drive the
REAL functions.
"""

from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Any

import pytest

from rebar_reconciler import apply_planning, reconcile


def _write_manifest(tmp_path: Path, outcomes: list[dict]) -> Path:
    p = tmp_path / "pass.manifest.json"
    p.write_text(json.dumps({"mutations": outcomes}))
    return p


def _mode_mod() -> Any:
    from rebar_reconciler import mode as mode_mod

    return mode_mod


def test_live_emit_returns_tally_and_still_removes_the_manifest(tmp_path: Path) -> None:
    """LIVE must surface the counts it is about to destroy.

    The "no manifest file in LIVE" contract is deliberate, so the fix must keep
    unlinking the file — but it must not throw away the tally with it.
    """
    outcomes = [
        {"action": "create", "key": "OK-1"},
        {"action": "create", "key": "BAD-1", "error": "stale-binding-404: HTTP Error 404"},
        {"action": "delete", "key": "BAD-2", "error": "stale-binding-404: HTTP Error 404"},
    ]
    manifest = _write_manifest(tmp_path, outcomes)
    mode_mod = _mode_mod()

    action, value = apply_planning._emit_mode_manifest(
        mode_mod.Mode.LIVE, mode_mod, outcomes, [], "pass-1", manifest, tmp_path, True
    )

    assert action == "RETURN", f"LIVE still returns early, got {action!r}"
    assert not manifest.exists(), (
        "the 'no manifest file in LIVE' contract must be preserved — the file is still removed"
    )
    assert value is not None, (
        "LIVE must return the applied/failed tally instead of None; returning None is what "
        "makes mutation_failures structurally 0 in production (bug c903)"
    )
    assert value.get("failed_count") == 2, (
        f"two outcomes carry an 'error' key, so failed_count must be 2, got {value!r}"
    )
    assert value.get("applied_count") == 1, (
        f"one outcome has no 'error' key, so applied_count must be 1, got {value!r}"
    )


def test_live_emit_merges_cutover_tally_deferred_skipped_recovered_degraded(
    tmp_path: Path,
) -> None:
    """LIVE must fold the coordinator route's cutover buckets into the returned tally.

    RP-03 S3 T3: when ``_apply_batch`` reroutes migrated non-create families through the
    coordinator+fuse surface, it stamps a ``cutover_tally`` block into the manifest
    carrying the fuse-held ``deferred`` / data ``skipped`` / ``recovered`` buckets and the
    degraded-pass exit signal — dispositions the legacy applied/failed pair cannot express.
    The LIVE branch must merge them so ``derive_pass_tally`` fires the degraded exit; without
    the merge a fuse-deferred pass with no hard failures would still report degraded=False.
    """
    outcomes = [
        {"action": "update", "key": "OK-1"},
        {"action": "delete", "key": "BAD-1", "error": "permanent: HTTP Error 400"},
    ]
    manifest = tmp_path / "pass.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "mutations": outcomes,
                "cutover_tally": {
                    "applied_count": 1,
                    "failed_count": 1,
                    "deferred_count": 3,
                    "skipped_count": 2,
                    "recovered_count": 1,
                    "degraded": True,
                    "buckets": {},
                },
            }
        )
    )
    mode_mod = _mode_mod()

    action, value = apply_planning._emit_mode_manifest(
        mode_mod.Mode.LIVE, mode_mod, outcomes, [], "pass-cut", manifest, tmp_path, True
    )

    assert action == "RETURN"
    assert not manifest.exists(), "the no-manifest-in-LIVE contract is still honored"
    assert value is not None
    # applied/failed still come from the on-disk mutation outcomes...
    assert value.get("applied_count") == 1
    assert value.get("failed_count") == 1
    # ...and the cutover buckets + degraded signal are merged in from cutover_tally.
    assert value.get("deferred_count") == 3, f"deferred must come from cutover_tally: {value!r}"
    assert value.get("skipped_count") == 2, f"skipped must come from cutover_tally: {value!r}"
    assert value.get("recovered_count") == 1, f"recovered must come from cutover_tally: {value!r}"
    assert value.get("degraded") is True, (
        f"a fuse-degraded pass must surface degraded=True so the exit is non-zero: {value!r}"
    )


def test_live_emit_without_cutover_tally_leaves_legacy_tally_unchanged(tmp_path: Path) -> None:
    """A pass with no reroute (no ``cutover_tally``) keeps the exact legacy LIVE tally —
    no deferred/skipped/recovered/degraded keys invented."""
    outcomes = [{"action": "create", "key": "OK-1"}]
    manifest = _write_manifest(tmp_path, outcomes)
    mode_mod = _mode_mod()

    _action, value = apply_planning._emit_mode_manifest(
        mode_mod.Mode.LIVE, mode_mod, outcomes, [], "pass-nocut", manifest, tmp_path, True
    )

    assert value == {"applied_count": 1, "failed_count": 0}, (
        f"absent cutover_tally must leave the legacy two-key tally untouched, got {value!r}"
    )


def test_persist_and_log_counts_failures_in_live_without_a_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real tally must reflect failures when LIVE left no manifest on disk.

    This is the seam every existing test skips: they stub `reconcile_once`'s RETURN
    value, so they never exercise `_persist_and_log`'s own counting.
    """
    ctx = reconcile._PassContext(pass_id="pass-1", repo_root=tmp_path)
    ctx.persist = True
    ctx.mutations = [{"action": "create"}, {"action": "create"}, {"action": "delete"}]
    # Snapshot advance runs before the tally and asserts both paths are set.
    ctx.curr_path = tmp_path / "curr.json"
    ctx.curr_path.write_text("{}")
    ctx.prev_path = tmp_path / "prev.json"

    # binding_store is absent; its failures are caught and logged, and it does not
    # participate in the tally under test. sync_logger needs a no-op recorder.
    class _Logger:
        def log(self, *a: Any, **k: Any) -> None: ...
        def close(self, *a: Any, **k: Any) -> None: ...
        def sync_pass_end(self, *a: Any, **k: Any) -> None: ...

    ctx.sync_logger = _Logger()
    ctx.manifest_path = None  # LIVE: the manifest was unlinked
    ctx.nowrite_plan = None
    ctx.apply_tally = {"applied_count": 1, "failed_count": 2}

    # Neutralise the persistence side-effects; only the tally is under test.
    monkeypatch.setattr(reconcile, "_save_and_commit_bindings", lambda *a, **k: None, raising=False)

    result = reconcile._persist_and_log(ctx)

    assert result["mutation_failures"] == 2, (
        "LIVE must report the two failed mutations; 0 here is what lets a degraded pass "
        f"exit 0 (bug c903). got {result!r}"
    )
    assert result["mutations_applied"] == 1, (
        "a failed mutation must not be counted as applied — that is the 85a1 structural lie "
        f"('applied N of N' while mutations failed). got {result!r}"
    )


def test_live_failure_reaches_a_non_zero_exit_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chain the real functions all the way to the exit code.

    The two tests above stop at `_persist_and_log`'s result dict, and every pre-existing
    fail-loud test stubs `reconcile_once` to RETURN a `mutation_failures` value. Neither
    covers the link that actually makes a degraded pass loud: `run_pass`'s
    `if failures > 0: return 1`. That link was unreachable in LIVE for the whole life of
    the bug, so a test that stops short of it would not have caught c903 — nor would it
    catch the link being severed again.

    Drives: _emit_mode_manifest (LIVE, real) -> _persist_and_log (real) -> run_pass (real).
    """
    from rebar_reconciler import __main__ as reconciler_main

    outcomes = [
        {"action": "create", "key": "OK-1"},
        {"action": "create", "key": "BAD-1", "error": "stale-binding-404: HTTP Error 404"},
    ]
    manifest = _write_manifest(tmp_path, outcomes)
    mode_mod = _mode_mod()

    # 1. real LIVE emission: unlinks the manifest, hands back the tally
    _action, tally = apply_planning._emit_mode_manifest(
        mode_mod.Mode.LIVE, mode_mod, outcomes, [], "pass-e2e", manifest, tmp_path, True
    )
    assert not manifest.exists()

    # 2. real tally, with no manifest on disk (the LIVE shape)
    class _Logger:
        def log(self, *a: Any, **k: Any) -> None: ...
        def close(self, *a: Any, **k: Any) -> None: ...
        def sync_pass_end(self, *a: Any, **k: Any) -> None: ...

    ctx = reconcile._PassContext(pass_id="pass-e2e", repo_root=tmp_path)
    ctx.persist = True
    ctx.mutations = outcomes
    ctx.curr_path = tmp_path / "curr.json"
    ctx.curr_path.write_text("{}")
    ctx.prev_path = tmp_path / "prev.json"
    ctx.sync_logger = _Logger()
    ctx.manifest_path = None
    ctx.nowrite_plan = None
    ctx.apply_tally = tally
    result = reconcile._persist_and_log(ctx)

    # 3. real run_pass over that result -> the exit code CI actually sees
    applier_stub = types.SimpleNamespace(
        RescheduleError=type("_R", (Exception,), {}), EXIT_RESCHEDULE=75
    )
    reconcile_stub = types.SimpleNamespace(reconcile_once=lambda *a, **k: result)
    monkeypatch.setattr(
        reconciler_main,
        "_try_load_step",
        lambda name: {"reconcile": reconcile_stub, "applier": applier_stub}.get(name),
    )
    rc = reconciler_main.run_pass(repo_root=tmp_path, pass_id="pass-e2e")

    assert rc == 1, (
        "a LIVE pass with a failed mutation must exit non-zero — this is the whole of "
        "e534's 'fail loud at the END', and the link that was dead in production. "
        f"tally={tally} result={result} rc={rc}"
    )


# RP-03 S3 T3 — the non-create CUTOVER tally drives the SAME degraded-exit machinery.
#
# The cutover replaces the legacy compound-replay tally with ``batch_dispatch``'s
# five-bucket projection (applied/failed/deferred/skipped/recovered). AC4 requires the
# EXACT applied/failed/deferred/skipped/recovered tallies plus the nonzero degraded-pass
# exit to be preserved. These tests prove the cutover tally flows through the EXISTING
# ``_persist_and_log`` → ``run_pass`` seam (a LIVE dict return routed to
# ``ctx.apply_tally``) and still exits non-zero when a mutation failed — no change to the
# reconcile pipeline itself.
# ════════════════════════════════════════════════════════════════════════════════


class _T3Clock:
    def now(self) -> int:
        return 0

    def sleep_ms(self, ms: int) -> None: ...


def _t3_budget_factory():
    from rebar_reconciler import retry_budget as rb

    def factory():
        return rb.RetryBudget(clock=_T3Clock(), jitter=lambda: 0.0)

    return factory


def _t3_plan(identity: str, action: str):
    from rebar_reconciler import mutation as m
    from rebar_reconciler import ticket_plan as tp

    mut = m.Mutation(
        direction=m.MutationDirection.outbound,
        action=getattr(m.MutationAction, action),
        target=identity,
        payload={},
        provenance={"src": "outbound"},
    )
    return tp.TicketPlan(
        identity=identity,
        mutations=(mut,),
        diagnostics=(),
        disposition=tp.PlanDisposition.mutate,
        observation_version="ov-1",
        payload={},
        dependencies=(),
        defer_reason=None,
    )


def _t3_cutover_report():
    """A cutover report with one applied and one budget-exhausted (failed) plan."""
    from rebar_reconciler import batch_dispatch, coordinator

    def execute(plan, _mutation):
        if plan.identity == "OK-1":
            return coordinator.AtomicSignal(status="applied")
        return coordinator.AtomicSignal(status="transient")

    plans = [_t3_plan("OK-1", "update"), _t3_plan("BAD-1", "delete")]
    return batch_dispatch.coordinate_and_fuse(
        plans,
        execute=execute,
        locate=lambda _i: {"provider": "jira", "endpoint": "https://a.example"},
        budget_factory=_t3_budget_factory(),
        now_ms=0,
    )


def test_cutover_five_bucket_tally_is_exact() -> None:
    """AC4: ``build_pass_tally`` over the cutover report yields the exact five buckets."""
    from rebar_reconciler import batch_dispatch

    tally = batch_dispatch.build_pass_tally(_t3_cutover_report())
    assert tally["applied_count"] == 1
    assert tally["failed_count"] == 1
    assert tally["deferred_count"] == 0
    assert tally["skipped_count"] == 0
    assert tally["recovered_count"] == 0


def test_cutover_tally_reaches_a_non_zero_exit_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: the cutover's five-bucket tally, threaded through the EXISTING LIVE seam
    (``ctx.apply_tally`` → ``_persist_and_log`` → ``run_pass``), preserves the nonzero
    degraded-pass exit — a failed mutation still makes the pass loud."""
    from rebar_reconciler import __main__ as reconciler_main
    from rebar_reconciler import batch_dispatch

    tally = batch_dispatch.build_pass_tally(_t3_cutover_report())

    class _Logger:
        def log(self, *a: Any, **k: Any) -> None: ...
        def close(self, *a: Any, **k: Any) -> None: ...
        def sync_pass_end(self, *a: Any, **k: Any) -> None: ...

    ctx = reconcile._PassContext(pass_id="pass-t3", repo_root=tmp_path)
    ctx.persist = True
    ctx.mutations = [{"action": "update"}, {"action": "delete"}]
    ctx.curr_path = tmp_path / "curr.json"
    ctx.curr_path.write_text("{}")
    ctx.prev_path = tmp_path / "prev.json"
    ctx.sync_logger = _Logger()
    ctx.manifest_path = None
    ctx.nowrite_plan = None
    ctx.apply_tally = tally
    monkeypatch.setattr(reconcile, "_save_and_commit_bindings", lambda *a, **k: None, raising=False)
    result = reconcile._persist_and_log(ctx)
    assert result["mutation_failures"] == 1
    assert result["mutations_applied"] == 1

    applier_stub = types.SimpleNamespace(
        RescheduleError=type("_R", (Exception,), {}), EXIT_RESCHEDULE=75
    )
    reconcile_stub = types.SimpleNamespace(reconcile_once=lambda *a, **k: result)
    monkeypatch.setattr(
        reconciler_main,
        "_try_load_step",
        lambda name: {"reconcile": reconcile_stub, "applier": applier_stub}.get(name),
    )
    rc = reconciler_main.run_pass(repo_root=tmp_path, pass_id="pass-t3")
    assert rc == 1


def _t3_degraded_deferred_report():
    """A cutover report where the fuse OPENS on budget-deferred (retryable_deferred)
    outcomes only: four plans on ONE endpoint each exhaust the cumulative-sleep budget,
    so the ``failed`` bucket stays empty yet the fuse opens — a degraded pass."""
    from rebar_reconciler import batch_dispatch, coordinator
    from rebar_reconciler import retry_budget as rb

    over = rb.MAX_CUMULATIVE_SLEEP_MS + 5000

    def execute(_plan, _mutation):
        return coordinator.AtomicSignal(
            status="transient", scope=coordinator.FailureScope.ticket, provider_delay_ms=over
        )

    plans = [_t3_plan(f"D-{i}", "update") for i in range(4)]
    return batch_dispatch.coordinate_and_fuse(
        plans,
        execute=execute,
        locate=lambda _i: {"provider": "jira", "endpoint": "https://a.example"},
        budget_factory=_t3_budget_factory(),
        now_ms=0,
    )


def test_cutover_deferred_only_pass_is_degraded_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: a degraded pass with ZERO failures (the fuse opened, so matching work was
    safety-deferred) must still exit non-zero. The tally carries ``degraded=True`` with
    ``failed_count == 0``; threaded through the LIVE seam it drives an OPERATIONAL exit."""
    from rebar_reconciler import __main__ as reconciler_main
    from rebar_reconciler import batch_dispatch

    report = _t3_degraded_deferred_report()
    tally = batch_dispatch.build_pass_tally(report)
    assert tally["failed_count"] == 0
    assert tally["deferred_count"] == 4
    assert tally["degraded"] is True

    class _Logger:
        def log(self, *a: Any, **k: Any) -> None: ...
        def close(self, *a: Any, **k: Any) -> None: ...
        def sync_pass_end(self, *a: Any, **k: Any) -> None: ...

    ctx = reconcile._PassContext(pass_id="pass-t3d", repo_root=tmp_path)
    ctx.persist = True
    ctx.mutations = [{"action": "update"}] * 4
    ctx.curr_path = tmp_path / "curr.json"
    ctx.curr_path.write_text("{}")
    ctx.prev_path = tmp_path / "prev.json"
    ctx.sync_logger = _Logger()
    ctx.manifest_path = None
    ctx.nowrite_plan = None
    ctx.apply_tally = tally
    monkeypatch.setattr(reconcile, "_save_and_commit_bindings", lambda *a, **k: None, raising=False)
    result = reconcile._persist_and_log(ctx)
    assert result["mutation_failures"] == 0
    assert result["mutations_deferred"] == 4
    assert result["degraded"] is True

    applier_stub = types.SimpleNamespace(
        RescheduleError=type("_R", (Exception,), {}), EXIT_RESCHEDULE=75
    )
    reconcile_stub = types.SimpleNamespace(reconcile_once=lambda *a, **k: result)
    monkeypatch.setattr(
        reconciler_main,
        "_try_load_step",
        lambda name: {"reconcile": reconcile_stub, "applier": applier_stub}.get(name),
    )
    rc = reconciler_main.run_pass(repo_root=tmp_path, pass_id="pass-t3d")
    assert rc == 1


def test_legacy_apply_tally_without_degraded_key_stays_converged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-cutover apply_tally (no ``deferred_count`` / ``degraded`` keys) is unchanged:
    the new fields default to 0 / absent so a clean legacy pass stays converged."""
    from rebar_reconciler import __main__ as reconciler_main

    class _Logger:
        def log(self, *a: Any, **k: Any) -> None: ...
        def close(self, *a: Any, **k: Any) -> None: ...
        def sync_pass_end(self, *a: Any, **k: Any) -> None: ...

    ctx = reconcile._PassContext(pass_id="pass-legacy", repo_root=tmp_path)
    ctx.persist = True
    ctx.mutations = [{"action": "update"}]
    ctx.curr_path = tmp_path / "curr.json"
    ctx.curr_path.write_text("{}")
    ctx.prev_path = tmp_path / "prev.json"
    ctx.sync_logger = _Logger()
    ctx.manifest_path = None
    ctx.nowrite_plan = None
    ctx.apply_tally = {"applied_count": 1, "failed_count": 0}
    monkeypatch.setattr(reconcile, "_save_and_commit_bindings", lambda *a, **k: None, raising=False)
    result = reconcile._persist_and_log(ctx)
    assert result["mutations_deferred"] == 0
    assert result["mutations_skipped"] == 0
    assert "degraded" not in result

    applier_stub = types.SimpleNamespace(
        RescheduleError=type("_R", (Exception,), {}), EXIT_RESCHEDULE=75
    )
    reconcile_stub = types.SimpleNamespace(reconcile_once=lambda *a, **k: result)
    monkeypatch.setattr(
        reconciler_main,
        "_try_load_step",
        lambda name: {"reconcile": reconcile_stub, "applier": applier_stub}.get(name),
    )
    rc = reconciler_main.run_pass(repo_root=tmp_path, pass_id="pass-legacy")
    assert rc == 0


# ════════════════════════════════════════════════════════════════════════════════
# ── S3T3-REROUTE-E2E-HELDOUT-START ── live cutover reroute, withheld from impl ──
#
# These drive the REAL ``applier.apply`` non-create dispatch with a fake transport and
# an explicit ``ticket_plans`` shadow set, pinning the OBSERVABLE cutover contract:
#   * a migrated family (update) physically dispatches through the S3 coordinator route
#     (``coordinate_and_fuse``) — NOT the legacy ``applier._apply_one`` batch loop
#     (AC1/AC5: legacy per-mutation nesting unreachable for migrated families);
#   * the ``rebar-id:<local_id>`` audit label is still stamped (G6 guard preserved);
#   * the per-mutation lost-lease ``abort_check`` still gates each physical op (G6);
#   * the cross-project (bug 626d) guard still fails the pass closed before any write.
# ════════════════════════════════════════════════════════════════════════════════

from unittest.mock import MagicMock, patch  # noqa: E402


def _rr_mods():
    from rebar_reconciler import applier, mutation, ticket_plan

    return applier, mutation, ticket_plan


def _rr_outbound_update(mutation_mod, target, local_id, fields):
    d = mutation_mod.MutationDirection
    a = mutation_mod.MutationAction
    return mutation_mod.Mutation(
        direction=d.outbound,
        action=a.update,
        target=target,
        payload={"changed_fields": dict(fields), "local_id": local_id},
        provenance={"src": "outbound"},
    )


def _rr_mutate_plan(ticket_plan_mod, identity, muts):
    return ticket_plan_mod.TicketPlan(
        identity=identity,
        mutations=tuple(muts),
        diagnostics=(),
        disposition=ticket_plan_mod.PlanDisposition("mutate"),
        observation_version="ov-e2e",
        payload={},
        dependencies=(),
        defer_reason=None,
    )


def _rr_manifest_outcomes(repo_root: Path, pass_id: str) -> list[dict]:
    p = repo_root / "bridge_state" / "snapshots" / f"{pass_id}.manifest.json"
    if not p.is_file():
        return []
    return json.loads(p.read_text(encoding="utf-8")).get("mutations", [])


def test_live_reroute_migrated_update_dispatches_through_coordinator_not_legacy_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    applier, mutation_mod, ticket_plan_mod = _rr_mods()
    m = _rr_outbound_update(mutation_mod, "DIG-100", "loc-100", {"summary": "x"})
    plan = _rr_mutate_plan(ticket_plan_mod, "DIG-100", [m])

    fake_client = MagicMock()
    fake_client.update_issue.return_value = {"key": "DIG-100", "ok": True}
    fake_client.search_issues.return_value = []

    seen_apply_one: list = []
    orig_apply_one = applier._apply_one

    def spy_apply_one(mutation, ctx, sink):
        seen_apply_one.append(mutation.get("key"))
        return orig_apply_one(mutation, ctx, sink)

    monkeypatch.setattr(applier, "_apply_one", spy_apply_one)

    # G6: the pre-dispatch rebar-id label-write authorization guard must still fire on
    # the coordinator route. The guard lives in its owning leaf module rebar_id_audit;
    # the coordinator dispatch loads it from there, so spy on the source function.
    from rebar_reconciler import rebar_id_audit

    audited: list = []
    orig_audit = rebar_id_audit._audit_rebar_id_label_writes

    def spy_audit(leaf, views):
        audited.append(leaf)
        return orig_audit(leaf, views)

    monkeypatch.setattr(rebar_id_audit, "_audit_rebar_id_label_writes", spy_audit)

    pass_id = "reroute-e2e-happy"
    with patch.object(applier, "_load_acli", return_value=fake_client):
        applier.apply([m], pass_id, repo_root=tmp_path, ticket_plans=[plan])

    # The physical write happened (coordinator route dispatched the update once)...
    assert fake_client.update_issue.called, "the migrated update must physically dispatch"
    # ...the rebar-id audit guard still ran for the migrated op (G6 preserved)...
    assert "outbound_update" in audited, (
        "the rebar-id label-write audit guard must still run on the coordinator route (G6)"
    )
    # ...and the migrated mutation did NOT traverse the legacy _apply_one batch loop.
    assert "DIG-100" not in seen_apply_one, (
        "a migrated family must not dispatch through the legacy per-mutation nesting (AC5)"
    )
    outcomes = _rr_manifest_outcomes(tmp_path, pass_id)
    assert any(o.get("key") == "DIG-100" and not o.get("error") for o in outcomes), (
        f"the coordinator route must record the applied outcome; got {outcomes}"
    )


def test_live_reroute_abort_check_stops_before_physical_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    applier, mutation_mod, ticket_plan_mod = _rr_mods()
    m = _rr_outbound_update(mutation_mod, "DIG-101", "loc-101", {"summary": "y"})
    plan = _rr_mutate_plan(ticket_plan_mod, "DIG-101", [m])

    fake_client = MagicMock()
    fake_client.search_issues.return_value = []

    class _LeaseLost(Exception):
        pass

    def abort_check():
        raise _LeaseLost("lease-lost")

    pass_id = "reroute-e2e-abort"
    with patch.object(applier, "_load_acli", return_value=fake_client):
        with pytest.raises(_LeaseLost):
            applier.apply(
                [m], pass_id, repo_root=tmp_path, ticket_plans=[plan], abort_check=abort_check
            )
    assert not fake_client.update_issue.called, (
        "a lost lease must abort BEFORE any physical mutation issues on the coordinator route"
    )


def test_live_reroute_cross_project_offender_fails_closed_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    applier, mutation_mod, ticket_plan_mod = _rr_mods()
    m = _rr_outbound_update(mutation_mod, "OTHER-1", "loc-other", {"summary": "z"})
    plan = _rr_mutate_plan(ticket_plan_mod, "OTHER-1", [m])

    fake_client = MagicMock()
    fake_client.search_issues.return_value = []

    pass_id = "reroute-e2e-xproj"
    with patch.object(applier, "_load_acli", return_value=fake_client):
        with pytest.raises(applier.CrossProjectTargetError):
            applier.apply([m], pass_id, repo_root=tmp_path, ticket_plans=[plan])
    assert not fake_client.update_issue.called, (
        "the cross-project (626d) guard must fail closed before any coordinator-route write"
    )


def test_live_reroute_head_drift_mid_batch_emits_abort_due_to_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A HeadDriftError raised while the coordinator route rechecks HEAD must still emit the
    structured ``abort_due_to_drift`` event, exactly as the legacy per-mutation loop does —
    the reroute runs INSIDE the drift-guarded try, not before it."""
    applier, mutation_mod, ticket_plan_mod = _rr_mods()
    m = _rr_outbound_update(mutation_mod, "DIG-200", "loc-200", {"summary": "d"})
    plan = _rr_mutate_plan(ticket_plan_mod, "DIG-200", [m])

    fake_client = MagicMock()
    fake_client.search_issues.return_value = []

    def hostile_drift(_concurrency, _repo_root, _pin):
        raise applier.HeadDriftError("hostile drift during reroute")

    monkeypatch.setattr(applier, "_recheck_drift", hostile_drift)

    pass_id = "reroute-e2e-drift"
    with patch.object(applier, "_load_acli", return_value=fake_client):
        with pytest.raises(applier.HeadDriftError):
            applier.apply([m], pass_id, repo_root=tmp_path, ticket_plans=[plan])

    err = capsys.readouterr().err
    assert "abort_due_to_drift" in err, (
        "a coordinator-route HEAD drift must emit the same abort_due_to_drift event as legacy"
    )
    assert not fake_client.update_issue.called, "drift aborts before the physical write"


def test_live_reroute_backstop_records_generic_dispatch_failure_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-mutation failure backstop parity: when the physical dispatch on the
    coordinator route raises an UNHANDLED non-fail-fast exception (a bare RuntimeError),
    it is recorded as a per-mutation ``error`` outcome (via record_backstop_failure) and
    the pass continues — exactly as the legacy ``_apply_one`` backstop does — instead of
    propagating and aborting the whole pass."""
    applier, mutation_mod, ticket_plan_mod = _rr_mods()
    m = _rr_outbound_update(mutation_mod, "DIG-300", "loc-300", {"summary": "boom"})
    plan = _rr_mutate_plan(ticket_plan_mod, "DIG-300", [m])

    fake_client = MagicMock()
    fake_client.search_issues.return_value = []
    fake_client.update_issue.side_effect = RuntimeError("unreachable transition")

    pass_id = "reroute-e2e-backstop"
    with patch.object(applier, "_load_acli", return_value=fake_client):
        # Must NOT raise — the backstop records and continues.
        applier.apply([m], pass_id, repo_root=tmp_path, ticket_plans=[plan])

    assert fake_client.update_issue.called, "the physical dispatch was attempted"
    outcomes = _rr_manifest_outcomes(tmp_path, pass_id)
    assert any(o.get("key") == "DIG-300" and o.get("error") for o in outcomes), (
        f"the coordinator route must record a backstop error outcome; got {outcomes}"
    )


def test_live_reroute_reraises_non_404_httperror_fail_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-fast parity: a non-404 ``HTTPError`` raised by the physical dispatch on the
    coordinator route propagates (aborting the pass), exactly as the legacy
    ``_apply_one`` re-raise arm does — the backstop deliberately does NOT swallow it."""
    import urllib.error

    applier, mutation_mod, ticket_plan_mod = _rr_mods()
    m = _rr_outbound_update(mutation_mod, "DIG-301", "loc-301", {"summary": "5xx"})
    plan = _rr_mutate_plan(ticket_plan_mod, "DIG-301", [m])

    fake_client = MagicMock()
    fake_client.search_issues.return_value = []
    fake_client.update_issue.side_effect = urllib.error.HTTPError(
        "https://a.example", 500, "server error", {}, None
    )

    pass_id = "reroute-e2e-httperror"
    with patch.object(applier, "_load_acli", return_value=fake_client):
        with pytest.raises(urllib.error.HTTPError):
            applier.apply([m], pass_id, repo_root=tmp_path, ticket_plans=[plan])


# ════════════════════════════════════════════════════════════════════════════════
# ── S3T3-REROUTE-E2E-HELDOUT-END ──
# ════════════════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════════════════
# REB-3115 S5 T2 — proven-state advancement + exact mixed-pass tallies (AC3/AC4/AC5)
# ════════════════════════════════════════════════════════════════════════════════
# The reconcile pass must advance cursor / baseline / binding state ONLY for a proven
# postcondition. Deferred / failed / skipped / commit_unknown work is NOT counted as
# applied and stays eligible for the next pass, and the mixed-pass five-bucket tally
# (applied / recovered / deferred / failed / skipped) plus the process exit are EXACT.


class _StubBindingStore:
    """A minimal binding store recording which bindings had a baseline advanced.

    ``set_baseline`` / ``merge_baseline`` are the two ADR-0026 advance seams; a binding
    only reaches them when its postcondition is PROVEN — the pass-start fetch for
    untouched fields, and the confirmed-landed ``synced_fields`` overlay for our own
    writes. A soft-failed / deferred write contributes to neither, so it must never
    appear in ``advanced`` / ``overlaid``.
    """

    def __init__(self, bindings: dict) -> None:
        self._bindings = bindings
        self.advanced: list[str] = []
        self.overlaid: list[str] = []
        self.saved = False

    def all_bindings(self) -> dict:
        return self._bindings

    def set_baseline(self, local_id: str, _value) -> None:
        self.advanced.append(local_id)

    def merge_baseline(self, local_id: str, _value) -> None:
        self.overlaid.append(local_id)

    def save(self) -> None:
        self.saved = True


def test_baseline_advances_only_for_proven_confirmed_writes() -> None:
    """AC3: the confirmed-landed ``synced_fields`` overlay advances the baseline ONLY for a
    binding whose write PROVABLY landed. A deferred/failed binding — absent from
    ``synced_fields`` — gets no overlay, so it stays eligible for the next pass."""
    store = _StubBindingStore(
        {
            "loc-applied": {"state": "confirmed", "jira_key": "DIG-1"},
            "loc-deferred": {"state": "confirmed", "jira_key": "DIG-2"},
        }
    )
    # Only the applied binding confirmedly landed a write this pass.
    synced_fields = {"loc-applied": {"summary": "landed"}}
    # Neither key is in the pass-start fetch window (so set_baseline is not the mechanism
    # under test here — only the proven-write overlay is).
    reconcile._advance_baselines(store, {}, synced_fields)

    assert store.overlaid == ["loc-applied"], (
        "only a proven confirmed write advances the baseline; a deferred/failed binding "
        f"must not be overlaid and stays eligible next pass. got {store.overlaid!r}"
    )
    assert "loc-deferred" not in store.overlaid


def _persist_tally(tmp_path: Path, monkeypatch, apply_tally: dict) -> dict:
    class _Logger:
        def __init__(self) -> None:
            self.events: list = []

        def log(self, event: str, **k: Any) -> None:
            self.events.append((event, k))

        def close(self, *a: Any, **k: Any) -> None: ...
        def sync_pass_end(self, *a: Any, **k: Any) -> None: ...

    ctx = reconcile._PassContext(pass_id="pass-mixed", repo_root=tmp_path)
    ctx.persist = True
    ctx.mutations = [{"action": "update"}] * (
        apply_tally.get("applied_count", 0)
        + apply_tally.get("failed_count", 0)
        + apply_tally.get("deferred_count", 0)
        + apply_tally.get("skipped_count", 0)
    )
    ctx.curr_path = tmp_path / "curr.json"
    ctx.curr_path.write_text("{}")
    ctx.prev_path = tmp_path / "prev.json"
    ctx.sync_logger = _Logger()
    ctx.manifest_path = None
    ctx.nowrite_plan = None
    ctx.apply_tally = apply_tally
    monkeypatch.setattr(reconcile, "_save_and_commit_bindings", lambda *a, **k: None, raising=False)
    return reconcile._persist_and_log(ctx)


def test_mixed_pass_tally_is_exact_including_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5: a mixed cutover pass surfaces EXACT, DISJOINT applied / recovered / deferred /
    failed / skipped counts that sum to ``mutation_count``.

    The production tally (``batch_dispatch.build_pass_tally``) FOLDS recovered into
    ``applied_count`` (``applied_count = applied + recovered`` — a legacy-consumer
    contract pinned by ``test_build_pass_tally_folds_recovered_into_applied``), so the
    apply_tally reconcile receives already carries recovered inside ``applied_count``.
    ``reconcile`` must subtract it back out so ``recovered`` is surfaced explicitly and
    NOT double-counted in ``mutations_applied``. Here 4 pure-applied + 5 recovered arrive
    as ``applied_count`` 9; the exact reported applied count is 4."""
    result = _persist_tally(
        tmp_path,
        monkeypatch,
        {
            # production-folded shape: 4 pure applied + 5 recovered == applied_count 9
            "applied_count": 9,
            "failed_count": 2,
            "deferred_count": 3,
            "skipped_count": 1,
            "recovered_count": 5,
            "degraded": True,
        },
    )
    assert result["mutations_applied"] == 4, (
        "recovered must be un-folded out of applied — mutations_applied is PURE applied"
    )
    assert result["mutation_failures"] == 2
    assert result["mutations_deferred"] == 3
    assert result["mutations_skipped"] == 1
    assert result["mutations_recovered"] == 5
    assert result["degraded"] is True
    # Exact tallies (AC5): the five DISJOINT buckets sum to the computed mutation count.
    assert (
        result["mutations_applied"]
        + result["mutations_recovered"]
        + result["mutation_failures"]
        + result["mutations_deferred"]
        + result["mutations_skipped"]
    ) == result["mutation_count"]


def test_reconcile_unfolds_the_real_build_pass_tally_fold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5 end-to-end: drive the REAL producer (``build_pass_tally``) into the REAL
    reconcile tally so the fold/un-fold pair is proven to compose, not a synthetic shape.

    A ``CutoverReport`` with 3 applied + 2 recovered projects (via the production
    ``build_pass_tally``) to ``applied_count`` 5 with ``recovered_count`` 2; reconcile must
    report ``mutations_applied`` 3 and ``mutations_recovered`` 2 — disjoint, summing to
    ``mutation_count``."""
    from rebar_reconciler import batch_dispatch

    report = batch_dispatch.CutoverReport(
        outcomes=(),
        tallies={"applied": 3, "recovered": 2, "deferred": 1, "failed": 0, "skipped": 0},
        fuse_decisions=(),
        degraded=False,
    )
    apply_tally = batch_dispatch.build_pass_tally(report)
    assert apply_tally["applied_count"] == 5, "guard: production folds recovered into applied"
    assert apply_tally["recovered_count"] == 2

    result = _persist_tally(tmp_path, monkeypatch, apply_tally)

    assert result["mutations_applied"] == 3
    assert result["mutations_recovered"] == 2
    assert result["mutations_deferred"] == 1
    assert result["mutation_failures"] == 0
    assert result["mutations_skipped"] == 0
    assert (
        result["mutations_applied"]
        + result["mutations_recovered"]
        + result["mutation_failures"]
        + result["mutations_deferred"]
        + result["mutations_skipped"]
    ) == result["mutation_count"]


def test_commit_unknown_deferred_work_is_not_counted_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: ``commit_unknown`` folds into the ``deferred`` bucket (failure_policy) — it is
    NOT counted as applied, so it remains eligible for the next pass. A pass whose only
    non-applied work is a single deferred commit_unknown still surfaces it as deferred and
    keeps ``applied`` exact."""
    result = _persist_tally(
        tmp_path,
        monkeypatch,
        {
            "applied_count": 1,
            "failed_count": 0,
            "deferred_count": 1,  # the commit_unknown mutation, folded to deferred
            "skipped_count": 0,
            "recovered_count": 0,
            "degraded": False,
        },
    )
    assert result["mutations_applied"] == 1, "commit_unknown must not be counted as applied"
    assert result["mutations_deferred"] == 1, "commit_unknown stays eligible as deferred work"


# ── last_pass: persist EXACT deferred/failed/recovered pass semantics (additive). ──


def _last_pass_mod() -> Any:
    from rebar_reconciler import last_pass

    return last_pass


def _git_repo(tmp_path: Path) -> Path:
    import subprocess

    repo = tmp_path / "lp_repo"
    repo.mkdir()
    for args in (
        ["init", "-q", "."],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "tester"],
        ["commit", "-q", "--allow-empty", "-m", "init"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return repo


def test_last_pass_persists_exact_disposition_tally_additively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2/AC5: the durable last-pass witness carries the EXACT deferred/failed/recovered
    pass disposition as an ADDITIVE, versioned block — the existing outcome/failure_kind
    fields are untouched so an older reader still validates the record."""
    last_pass = _last_pass_mod()
    repo = _git_repo(tmp_path)
    monkeypatch.setenv("REBAR_ENV_ID", "reconciler")

    record = last_pass.publish(
        repo,
        pass_id="pass-disp",
        outcome="failure",
        failure_kind="operational_failure",
        outcome_tally={
            "applied": 4,
            "recovered": 5,
            "deferred": 3,
            "failed": 2,
            "skipped": 1,
        },
        degraded=True,
    )

    # additive block present and exact
    assert record["outcome"] == "failure"
    assert record["failure_kind"] == "operational_failure"
    assert record["outcome_tally"] == {
        "applied": 4,
        "recovered": 5,
        "deferred": 3,
        "failed": 2,
        "skipped": 1,
    }
    assert record["degraded"] is True

    # the persisted witness re-reads and re-validates (round-trips through the ref).
    snap = last_pass.snapshot(repo, target_environment_id="reconciler")
    assert snap["outcome_tally"]["deferred"] == 3
    assert snap["outcome_tally"]["recovered"] == 5


def test_last_pass_reads_a_legacy_record_without_the_additive_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC (old readers work): a record published WITHOUT the additive disposition block is
    still valid and readable — the new fields default to null rather than failing
    validation."""
    last_pass = _last_pass_mod()
    repo = _git_repo(tmp_path)
    monkeypatch.setenv("REBAR_ENV_ID", "reconciler")

    record = last_pass.publish(repo, pass_id="pass-legacy", outcome="success")
    assert record["outcome"] == "success"
    assert record.get("outcome_tally") is None

    snap = last_pass.snapshot(repo, target_environment_id="reconciler")
    assert snap["verdict"] in {"HEALTHY", "NEVER_RUN", "RUNNING", "PAUSED"}
    assert snap.get("outcome_tally") is None
