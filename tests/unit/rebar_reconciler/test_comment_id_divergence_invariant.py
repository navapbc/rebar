"""Reconciler outbound-comment invariant (ticket 0fa2-fdcb-229e-41a0).

Every successfully posted outbound Jira comment must have its returned comment
ID recorded locally. A pass that posted one or more comments while the binding
store's ``comment_ids`` map gained no entries is DIVERGENT. Alert firing is
debounced across 2 CONSECUTIVE reconcile passes (operator-ratified 2026-08-22,
N-of-M alarm philosophy): a single divergent pass records state but stays
silent; the same divergence observed on the next pass fires a ``bridge_alerts``
record; a healthy pass in between resets the counter.

Hermetic: real ``BindingStore`` over a tmp tracker dir, no Jira, no network.
Follows the reconciler test-tree loader convention (spec_from_file_location).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SRC_DIR = Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SRC_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def invariants_mod():
    return _load("_invariants_for_comment_divergence", "invariants.py")


@pytest.fixture(scope="module")
def binding_store_mod():
    return _load("_binding_store_for_comment_divergence", "binding_store.py")


@pytest.fixture
def repo_root(tmp_path):
    return tmp_path


def _fresh_store(binding_store_mod, repo_root: Path):
    """A new per-pass BindingStore over the same tracker dir (as reconcile does)."""
    return binding_store_mod.BindingStore(repo_root / ".tickets-tracker")


def _alert_records(repo_root: Path) -> list[dict]:
    alerts_dir = repo_root / "bridge_state" / "bridge_alerts"
    records: list[dict] = []
    if not alerts_dir.is_dir():
        return records
    for jf in sorted(alerts_dir.glob("*.jsonl")):
        for line in jf.read_text(encoding="utf-8").splitlines():
            records.append(json.loads(line))
    return records


def _divergence_records(repo_root: Path) -> list[dict]:
    return [r for r in _alert_records(repo_root) if r.get("kind") == "comment-id-divergence"]


# ── pure debounce step ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("prev", "divergent", "expected"),
    [
        (0, False, (0, False)),  # idle/healthy stays at zero
        (0, True, (1, False)),  # first divergent observation: count, don't fire
        (1, True, (2, True)),  # second consecutive: fire
        (2, True, (3, True)),  # divergent past the threshold: keep firing (deduped downstream)
        (1, False, (0, False)),  # healthy observation resets
        (5, False, (0, False)),  # reset from any depth
    ],
)
def test_step_comment_divergence_table(invariants_mod, prev, divergent, expected):
    assert invariants_mod.step_comment_divergence(prev, divergent) == expected


# ── posted-counter seam ───────────────────────────────────────────────────────


def test_binding_store_counts_posts_and_map_growth(binding_store_mod, repo_root):
    """The facade tallies posts per pass and reports comment_ids growth since load."""
    store = _fresh_store(binding_store_mod, repo_root)
    assert store.comment_posts_this_pass() == 0
    assert store.comment_ids_gained_this_pass() == 0

    store.note_comment_posted()
    store.record_comment_id("hlc-1", "10001")

    assert store.comment_posts_this_pass() == 1
    assert store.comment_ids_gained_this_pass() == 1

    # A fresh store over the same tracker re-baselines: prior entries are not "gained".
    reopened = _fresh_store(binding_store_mod, repo_root)
    assert reopened.comment_posts_this_pass() == 0
    assert reopened.comment_ids_gained_this_pass() == 0


def test_record_comment_id_seam_counts_post_even_without_key(binding_store_mod, repo_root):
    """_record_comment_id tallies the successful post BEFORE its recording guards.

    An entry with no ``local_comment_key`` means recording no-ops — exactly the
    silent-failure class this invariant watches — so the post must still count.
    """
    from rebar_reconciler.dispatch_apply_phases import _record_comment_id

    store = _fresh_store(binding_store_mod, repo_root)
    _record_comment_id(store, {"body": "no key"}, {"id": "10001"})

    assert store.comment_posts_this_pass() == 1
    assert store.comment_ids_gained_this_pass() == 0


# ── debounced end-of-pass check ───────────────────────────────────────────────


def _divergent_pass(invariants_mod, binding_store_mod, repo_root: Path, pass_id: str):
    """Simulate a pass that posted a comment whose recording silently no-oped."""
    store = _fresh_store(binding_store_mod, repo_root)
    store.note_comment_posted()
    return invariants_mod.check_comment_id_recording(store, repo_root, pass_id)


def _healthy_pass(invariants_mod, binding_store_mod, repo_root: Path, pass_id: str, hlc: str):
    """Simulate a pass that posted a comment AND recorded its id."""
    store = _fresh_store(binding_store_mod, repo_root)
    store.note_comment_posted()
    store.record_comment_id(hlc, "20002")
    return invariants_mod.check_comment_id_recording(store, repo_root, pass_id)


def test_single_divergent_pass_does_not_alert(invariants_mod, binding_store_mod, repo_root):
    """(a) divergence observed once -> state recorded, NO alert."""
    result = _divergent_pass(invariants_mod, binding_store_mod, repo_root, "pass-1")

    assert result["divergent"] is True
    assert result["consecutive"] == 1
    assert result["alert_fired"] is False
    assert _divergence_records(repo_root) == []


def test_two_consecutive_divergent_passes_alert(invariants_mod, binding_store_mod, repo_root):
    """(b) the same divergence in 2 consecutive passes -> alert fires, names the
    invariant and the pass."""
    _divergent_pass(invariants_mod, binding_store_mod, repo_root, "pass-1")
    result = _divergent_pass(invariants_mod, binding_store_mod, repo_root, "pass-2")

    assert result["consecutive"] == 2
    assert result["alert_fired"] is True
    records = _divergence_records(repo_root)
    assert len(records) == 1
    rec = records[0]
    assert rec["key"] == "bridge-alert:comment-id-divergence"
    assert rec["pass_id"] == "pass-2"
    assert "comment-id-recording" in rec["message"]
    assert "pass-2" in rec["message"]


def test_healthy_pass_between_divergences_resets(invariants_mod, binding_store_mod, repo_root):
    """(c) divergence resolved between passes -> counter resets, NO alert."""
    _divergent_pass(invariants_mod, binding_store_mod, repo_root, "pass-1")
    healthy = _healthy_pass(invariants_mod, binding_store_mod, repo_root, "pass-2", "hlc-h1")
    result = _divergent_pass(invariants_mod, binding_store_mod, repo_root, "pass-3")

    assert healthy["divergent"] is False
    assert healthy["consecutive"] == 0
    assert result["consecutive"] == 1
    assert result["alert_fired"] is False
    assert _divergence_records(repo_root) == []


def test_idle_pass_is_healthy(invariants_mod, binding_store_mod, repo_root):
    """A pass with no posts observes nothing and resets the counter."""
    _divergent_pass(invariants_mod, binding_store_mod, repo_root, "pass-1")
    store = _fresh_store(binding_store_mod, repo_root)
    result = invariants_mod.check_comment_id_recording(store, repo_root, "pass-2")

    assert result["divergent"] is False
    assert result["consecutive"] == 0
    assert _divergence_records(repo_root) == []


def test_alert_is_deduped_within_window(invariants_mod, binding_store_mod, repo_root):
    """A third consecutive divergent pass does not append a second record inside
    the alert store's 24h dedup window."""
    _divergent_pass(invariants_mod, binding_store_mod, repo_root, "pass-1")
    _divergent_pass(invariants_mod, binding_store_mod, repo_root, "pass-2")
    result = _divergent_pass(invariants_mod, binding_store_mod, repo_root, "pass-3")

    assert result["consecutive"] == 3
    assert result["alert_fired"] is False
    assert len(_divergence_records(repo_root)) == 1


def test_corrupt_state_file_fails_open(invariants_mod, binding_store_mod, repo_root):
    """A corrupt debounce-state file degrades to counter 0 without raising."""
    state = repo_root / "bridge_state" / "comment_id_divergence.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{not json", encoding="utf-8")

    result = _divergent_pass(invariants_mod, binding_store_mod, repo_root, "pass-1")

    assert result["consecutive"] == 1
    assert result["alert_fired"] is False


def test_legacy_store_without_counters_is_a_noop(invariants_mod, repo_root):
    """A store predating the counters (or a bare test double) degrades to a no-op."""
    result = invariants_mod.check_comment_id_recording(object(), repo_root, "pass-1")

    assert result["divergent"] is False
    assert result["alert_fired"] is False
    assert _divergence_records(repo_root) == []
