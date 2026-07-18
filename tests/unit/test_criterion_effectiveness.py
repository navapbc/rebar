"""Golden tests for the standing per-criterion effectiveness recorder (epic 6982, extends R7).

The recorder lives under ``docs/experiments/plan-review-gate/harnesses/`` (not a package), so — as
with ``test_gate_eval_classifier.py`` — we load it by path. This pins the CI-verifiable core: the
pure ``firings_from_review`` extraction, the ``compute_effectiveness`` detection/de-escalation/
window/zero-denominator logic (on synthetic fixtures with known round sequences), criterion
auto-inclusion, and a regression that the committed metrics artifact is exactly reproducible from
the committed firing ledger and is non-trivial over the real corpus. The live-store ``--record``
path is exercised out-of-band (the AC proving commands), mirroring R7's ``--verify-repro`` posture.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_HARNESS = _ROOT / "docs/experiments/plan-review-gate/harnesses/criterion_effectiveness.py"
_spec = importlib.util.spec_from_file_location("criterion_effectiveness", _HARNESS)
assert _spec and _spec.loader
ce = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ce)


def _row(t, ts, d, crit, u, *, v="PASS", n=None, x=None):
    """Build one firing row (short-key schema). ``n`` defaults to a per-(u,d,ts) unique id so
    distinct findings never accidentally share a dedup fingerprint."""
    return {
        "t": t,
        "ts": ts,
        "r": f"uuid-{ts}",
        "v": v,
        "c": list(crit),
        "n": n or f"n-{u}-{d}-{ts}",
        "u": u,
        "d": d,
        "s": "major",
        "p": 0.7,
        "x": x,
    }


# ── parse_sidecar_name ────────────────────────────────────────────────────────────────────────


def test_parse_sidecar_name():
    name = "1783824421901244001-45444fdb-a9ba-4111-9ad4-bcffe20e34d7-REVIEW_RESULT.json"
    ts, uuid = ce.parse_sidecar_name(name)
    assert ts == 1783824421901244001
    assert uuid == "45444fdb-a9ba-4111-9ad4-bcffe20e34d7"
    assert ce.parse_sidecar_name("SNAPSHOT.json") is None
    assert ce.parse_sidecar_name(".hidden-REVIEW_RESULT.json") is None
    assert ce.parse_sidecar_name("notanumber-uuid-REVIEW_RESULT.json") is None


# ── firings_from_review (pure, injected fingerprint fns; skips indeterminate abstains) ───────────


def test_firings_from_review_one_row_per_finding_skips_indeterminate():
    payload = {
        "verdict": "BLOCK",
        "findings": [
            {
                "criteria": ["E4"],
                "decision": "block",
                "severity": "major",
                "priority": 0.8,
                "location": "loc1",
                "finding": "f1",
                "drop_reason": None,
            },
            {
                "criteria": ["G6", "E4"],
                "decision": "advisory",
                "severity": "minor",
                "priority": 0.4,
                "location": "loc2",
                "finding": "f2",
                "drop_reason": None,
            },
            {"criteria": ["G6"], "decision": "indeterminate", "location": "loc3", "finding": "f3"},
        ],
    }
    rows = ce.firings_from_review(
        "tkt-1",
        42,
        "uuidX",
        payload,
        fix_unit_key=lambda f: "u:" + f["location"],
        norm_id=lambda f: "n:" + f["finding"],
    )
    assert len(rows) == 2  # the indeterminate abstain is dropped
    assert rows[0] == {
        "t": "tkt-1",
        "ts": 42,
        "r": "uuidX",
        "v": "BLOCK",
        "c": ["E4"],
        "n": "n:f1",
        "u": "u:loc1",
        "d": "block",
        "s": "major",
        "p": 0.8,
        "x": None,
    }
    assert rows[1]["c"] == ["G6", "E4"] and rows[1]["d"] == "advisory"


def test_firings_from_review_prefers_stored_norm_id():
    payload = {
        "verdict": "PASS",
        "findings": [
            {
                "criteria": ["T8"],
                "decision": "advisory",
                "norm_id": "STORED",
                "location": "l",
                "finding": "x",
            }
        ],
    }
    rows = ce.firings_from_review(
        "t", 1, "u", payload, fix_unit_key=lambda f: "u", norm_id=lambda f: "COMPUTED"
    )
    assert rows[0]["n"] == "STORED"


# ── compute_effectiveness: detection / de-escalation / window / zero-denominator ────────────────


def test_detection_proxy_counts_block_remediated_to_pass():
    # X's fix-unit u1 blocks in round 1, is ABSENT from round 2 (author fixed it), round 2 is PASS.
    rows = [
        _row("T1", 1, "block", ["X"], "u1", v="BLOCK"),
        _row("T1", 2, "advisory", ["Y"], "u2", v="PASS"),  # round 2 exists; u1 gone
    ]
    m = ce.compute_effectiveness(rows, window=None)
    assert m["X"]["detection_proxy"] == 1.0
    assert m["X"]["blocking_fp_proxy"] == 0.0  # not de-escalated
    assert m["X"]["sample_counts"] == {
        "blocking_fix_units": 1,
        "resolvable_fix_units": 1,
        "remediated": 1,
        "deescalated": 0,
        "advisory_firings": 0,
        "tickets": 1,
        "firings": 1,
    }


def test_blocking_fp_proxy_counts_gate_de_escalation():
    # X's fix-unit u1 blocks in round 1, then in round 2 the gate DROPS it (found again, not
    # surfaced as blocking) — a reversal, never remediated → FP proxy fires; detection is 0.
    rows = [
        _row("T2", 1, "block", ["X"], "u1", v="BLOCK", n="same"),
        _row("T2", 2, "dropped", ["X"], "u1", v="PASS", n="same"),
    ]
    m = ce.compute_effectiveness(rows, window=None)
    assert m["X"]["blocking_fp_proxy"] == 1.0
    assert m["X"]["detection_proxy"] == 0.0
    assert m["X"]["sample_counts"]["deescalated"] == 1


def test_persisting_block_is_neither_detection_nor_fp():
    # u1 blocks in both rounds (still open): not remediated, not de-escalated.
    rows = [
        _row("T3", 1, "block", ["X"], "u1", v="BLOCK", n="k"),
        _row("T3", 2, "block", ["X"], "u1", v="BLOCK", n="k"),
    ]
    m = ce.compute_effectiveness(rows, window=None)
    assert m["X"]["detection_proxy"] == 0.0  # resolvable but not remediated
    assert m["X"]["blocking_fp_proxy"] == 0.0


def test_single_round_block_is_not_resolvable_for_detection():
    # A blocking fix-unit on a one-round ticket has no observable disposition → not resolvable, so
    # it does not enter the detection denominator, but it IS a blocking fix-unit for the FP denom.
    rows = [_row("T4", 1, "block", ["X"], "u1", v="BLOCK")]
    m = ce.compute_effectiveness(rows, window=None)
    assert m["X"]["detection_proxy"] is None  # zero resolvable denominator → null, not 0.0
    assert m["X"]["blocking_fp_proxy"] == 0.0
    assert m["X"]["sample_counts"]["blocking_fix_units"] == 1
    assert m["X"]["sample_counts"]["resolvable_fix_units"] == 0


def test_auto_includes_unseen_criterion():
    # A criterion id no prior fixture or registry mentions is auto-included with no wiring — the
    # property that lets R1/R3/R4's new advisory criteria flow in for free.
    rows = [_row("T5", 1, "advisory", ["BRAND_NEW_R99"], "u1", v="PASS")]
    m = ce.compute_effectiveness(rows, window=None)
    assert "BRAND_NEW_R99" in m
    # advisory-only criterion → both blocking proxies are null (zero blocking denominator).
    assert m["BRAND_NEW_R99"]["detection_proxy"] is None
    assert m["BRAND_NEW_R99"]["blocking_fp_proxy"] is None
    assert m["BRAND_NEW_R99"]["sample_counts"]["advisory_firings"] == 1


def test_window_bounds_tickets_by_recency():
    rows = [
        _row("old", 10, "advisory", ["OLD"], "u1", v="PASS"),
        _row("mid", 20, "advisory", ["MID"], "u2", v="PASS"),
        _row("new", 30, "advisory", ["NEW"], "u3", v="PASS"),
    ]
    m1 = ce.compute_effectiveness(rows, window=1)
    assert set(m1) == {"NEW"}  # only the most-recently-reviewed ticket survives
    m2 = ce.compute_effectiveness(rows, window=2)
    assert set(m2) == {"NEW", "MID"}
    m_all = ce.compute_effectiveness(rows, window=None)
    assert set(m_all) == {"OLD", "MID", "NEW"}


# ── regression: the committed metrics artifact over the real corpus ─────────────────────────────


def test_committed_metrics_nontrivial_over_existing_corpus():
    # The committed metrics artifact (computed over the EXISTING sidecar corpus) is the CI-visible
    # proof it works NOW — not future data. (The firing ledger itself is git-ignored: it is ~8 MB
    # over the current corpus, far over the 500 KB large-file gate, and is regenerated locally.)
    assert ce.METRICS_OUT.exists(), "committed criterion_effectiveness.json must be present"
    committed = json.loads(ce.METRICS_OUT.read_text())
    assert len(committed) >= 30  # auto-included criteria
    assert sum(1 for v in committed.values() if v["detection_proxy"] is not None) >= 1
    assert sum(1 for v in committed.values() if v["blocking_fp_proxy"] is not None) >= 1
    # Every entry carries the full self-verifying sample_counts shape.
    for v in committed.values():
        sc = v["sample_counts"]
        assert {
            "blocking_fix_units",
            "resolvable_fix_units",
            "remediated",
            "deescalated",
            "advisory_firings",
            "tickets",
            "firings",
        } <= set(sc)


def test_local_ledger_reproduces_committed_metrics():
    # When the local ledger is present (a dev/standing host that ran `--record`), the committed
    # metrics must be EXACTLY reproducible from it — a drift guard. Skipped in CI, where the
    # git-ignored ledger is absent.
    ledger = ce.load_firings()
    if not ledger:
        import pytest

        pytest.skip("firing ledger absent (git-ignored; regenerate with --record --backfill)")
    committed = json.loads(ce.METRICS_OUT.read_text())
    recomputed = ce.compute_effectiveness(ledger, window=ce.DEFAULT_WINDOW)
    assert json.loads(json.dumps(recomputed, sort_keys=True)) == committed
