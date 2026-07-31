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
