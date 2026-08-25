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
