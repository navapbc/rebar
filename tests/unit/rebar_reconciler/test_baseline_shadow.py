"""Baseline dual-write shadow — convergence rollout Phase 1 (epic 3006-e198 / 7d23).

RED-first cells for the derisk mechanism that runs the per-binding baseline in
SHADOW (advanced every pass, consumed by no one) and logs an equivalence check vs
prev_snapshot, so the prev_snapshot→baseline consumer swap is gated on N clean
passes. Follows the reconciler test-tree loader convention.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SRC / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_shadow = _load("_baseline_shadow_ut", "baseline_shadow.py")
_bs = _load("_binding_store_shadow_ut", "binding_store.py")
BindingStore = _bs.BindingStore
run = _shadow.run_dual_write_shadow


class _RecordingLogger:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def log(self, name, **kw):
        self.events.append((name, kw))


def _store(tmp_path: Path, bindings: dict[str, str]) -> BindingStore:
    bs = BindingStore(tmp_path / ".tickets-tracker")
    for lid, jk in bindings.items():
        bs.bind_confirm(lid, jk)
    bs.save()
    return bs


def test_first_pass_seeds_then_advances_baseline(tmp_path: Path) -> None:
    """With no prior baseline, the first shadow pass SEEDS (no comparison) and
    advances the baseline to the current snapshot value."""
    bs = _store(tmp_path, {"loc-1": "REB-1"})
    curr = {"REB-1": {"summary": "v1", "status": {"name": "To Do"}, "extra": "ignored"}}
    prev: dict = {}
    logger = _RecordingLogger()
    rec = run(bs, curr, prev, sync_logger=logger)
    assert rec["seeded"] == 1
    assert rec["divergent"] == 0
    # Baseline now holds the mirrored fields from curr.
    baseline = bs.get_baseline("loc-1")
    assert baseline["summary"] == "v1"
    assert baseline["status"] == {"name": "To Do"}
    assert "extra" not in baseline  # filtered to the mirrored fields
    assert logger.events[0][0] == "baseline_shadow_check"


def test_steady_state_baseline_equals_prev_snapshot(tmp_path: Path) -> None:
    """After a pass advances both, the next pass finds the stored baseline EQUAL to
    prev_snapshot (the live consumer's source) — a clean shadow pass."""
    bs = _store(tmp_path, {"loc-1": "REB-1"})
    fields = {
        "summary": "v1",
        "description": "d",
        "priority": {"name": "High"},
        "status": {"name": "To Do"},
        "assignee": None,
    }
    # Pass 1: seed the baseline from curr.
    run(bs, {"REB-1": fields}, {}, sync_logger=None)
    # Pass 2: prev_snapshot has advanced to pass-1's curr; curr is unchanged.
    logger = _RecordingLogger()
    rec = run(bs, {"REB-1": fields}, {"REB-1": fields}, sync_logger=logger)
    assert rec["equal"] == 1
    assert rec["divergent"] == 0


def test_divergence_between_baseline_and_prev_is_flagged(tmp_path: Path) -> None:
    """If the stored baseline disagrees with prev_snapshot on a mirrored field, the
    pass counts it divergent and emits a divergence event (the persistence-bug
    class behind drift B — the swap must not proceed while this is nonzero)."""
    bs = _store(tmp_path, {"loc-1": "REB-1"})
    bs.set_baseline("loc-1", {"summary": "OLD", "status": {"name": "To Do"}})
    curr = {"REB-1": {"summary": "NEW", "status": {"name": "To Do"}}}
    prev = {"REB-1": {"summary": "DIFFERENT", "status": {"name": "To Do"}}}
    logger = _RecordingLogger()
    rec = run(bs, curr, prev, sync_logger=logger)
    assert rec["divergent"] == 1
    assert "REB-1" in rec["divergent_keys"]
    names = [e[0] for e in logger.events]
    assert "baseline_shadow_check" in names
    assert "baseline_shadow_divergence" in names
    # And it still dual-writes (advances) the baseline to curr.
    assert bs.get_baseline("loc-1")["summary"] == "NEW"


def test_out_of_window_binding_is_skipped(tmp_path: Path) -> None:
    """A confirmed binding whose Jira key is absent from the current window has no
    fresh value to advance to — it is skipped (never mis-seeded from nothing)."""
    bs = _store(tmp_path, {"loc-1": "REB-1", "loc-2": "REB-2"})
    curr = {"REB-1": {"summary": "v1", "status": {"name": "To Do"}}}  # REB-2 absent
    rec = run(bs, curr, {}, sync_logger=None)
    assert rec["seeded"] == 1  # only REB-1
    assert bs.get_baseline("loc-2") is None  # untouched


def test_shadow_never_touches_the_live_consumer(tmp_path: Path) -> None:
    """The shadow only mutates baselines — it does NOT modify prev_snapshot (the
    live consumer's source), so enabling it cannot change arbitration."""
    bs = _store(tmp_path, {"loc-1": "REB-1"})
    prev = {"REB-1": {"summary": "prev"}}
    prev_before = dict(prev["REB-1"])
    run(bs, {"REB-1": {"summary": "curr"}}, prev, sync_logger=None)
    assert prev["REB-1"] == prev_before  # prev_snapshot untouched


def test_recon_baseline_shadow_check_line_emitted_to_stderr(tmp_path: Path, capsys) -> None:
    """Story a118: the shadow check prints a `RECON: baseline_shadow_check divergent=N`
    line to STDERR (even with no sync_logger) so the >=10-clean-pass rollout streak is
    derivable from the GHA reconcile-bridge run logs."""
    bs = _store(tmp_path, {"loc-1": "REB-1"})
    bs.set_baseline("loc-1", {"summary": "OLD", "status": {"name": "To Do"}})
    curr = {"REB-1": {"summary": "NEW", "status": {"name": "To Do"}}}
    prev = {"REB-1": {"summary": "DIFFERENT", "status": {"name": "To Do"}}}
    rec = run(bs, curr, prev, sync_logger=None)
    err = capsys.readouterr().err
    assert f"RECON: baseline_shadow_check divergent={rec['divergent']}" in err
    assert "equal=" in err and "seeded=" in err
