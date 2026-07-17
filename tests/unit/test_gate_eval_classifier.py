"""R7 (epic 6982): golden test for the gate-eval instrumentation classifier + metrics.

The instrumentation job lives under ``docs/experiments/plan-review-gate/harnesses/`` (not a
package), so we load it by path. This test pins the CI-verifiable core: the pure ``classify``
reproduces the frozen §5.2 labels at >=6/8, ``compute_metrics`` yields non-trivial values on a
fixture, and ``--window`` bounds the corpus. The full live-store reproduction is verified
manually via ``gate_eval_instrumentation.py --verify-repro``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_HARNESS = (
    Path(__file__).resolve().parents[2]
    / "docs/experiments/plan-review-gate/harnesses/gate_eval_instrumentation.py"
)
_spec = importlib.util.spec_from_file_location("gate_eval_instrumentation", _HARNESS)
assert _spec and _spec.loader
gei = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gei)


# The 8 frozen §5.2 cases as (post_claim_edit_class, review_round_count, has_strong, gold_label).
FROZEN = {
    "dc58": ("ac-strengthened", 1, False, "MISSED"),
    "db7b": ("ac-strengthened", 1, False, "MISSED"),
    "5886": ("ac-strengthened", 1, False, "MISSED"),
    "c8cc": ("substantive-unclassified", 1, False, "CAUGHT-BUT-IGNORED"),
    "f5df": ("ac-strengthened", 3, False, "CAUGHT-BUT-IGNORED"),
    "115b": ("operator-attested-retag", 1, False, "CAUGHT-BUT-IGNORED"),
    "8c4f": ("operator-attested-retag", 2, True, "CAUGHT-BUT-IGNORED"),
    "3006": ("ac-strengthened", 1, True, "UNKNOWABLE"),
}


def test_classify_reproduces_frozen_labels():
    preds = {k: gei.classify(cls, rnd, strong) for k, (cls, rnd, strong, _) in FROZEN.items()}
    agree = sum(preds[k] == FROZEN[k][3] for k in FROZEN)
    assert agree >= gei.REPRO_FLOOR  # the R7 acceptance floor (>=6/8)
    assert agree == 7  # the documented core-cascade result
    # the sole known miss is c8cc (a lone substantive-unclassified case, deliberately not overfit)
    assert [k for k in FROZEN if preds[k] != FROZEN[k][3]] == ["c8cc"]


def test_classify_na_when_no_post_claim_signal():
    assert gei.classify("none", 1, False) == "N/A"
    assert gei.classify("cosmetic", 1, False) == "N/A"
    assert gei.classify("none", 2, False) == "CAUGHT-BUT-IGNORED"  # >=2 rounds is a signal
    assert gei.classify("ac-strengthened", 1, False, had_persisted_review=False) == "N/A"


def test_dedup_union_counts_persistent_finding_once():
    payloads = [
        {"findings": [{"criteria": ["G6"], "finding": "same text", "decision": "advisory"}]},
        {"findings": [{"criteria": ["G6"], "finding": "same text", "decision": "advisory"}]},
        {"findings": [{"criteria": ["E4"], "finding": "other", "decision": "block"}]},
    ]
    union = gei.dedup_union(payloads)
    assert len(union) == 2  # the persistent (G6, "same text") finding counts once


def test_compute_metrics_nontrivial():
    rows = [
        {
            "ticket_id": "t1",
            "post_claim_edit_class": "ac-strengthened",
            "reopen_count": 0,
            "force_close": True,
        },
        {
            "ticket_id": "t2",
            "post_claim_edit_class": "none",
            "reopen_count": 1,
            "force_close": False,
        },
        {
            "ticket_id": "t3",
            "post_claim_edit_class": "none",
            "reopen_count": 0,
            "force_close": False,
        },
        {
            "ticket_id": "t4",
            "post_claim_edit_class": "ac-strengthened",
            "reopen_count": 0,
            "force_close": False,
        },
    ]
    fbt = {
        "t1": [
            {
                "criteria": ["G6"],
                "decision": "block",
                "priority": 0.9,
                "severity": "critical",
                "finding": "a",
            }
        ],
        "t2": [
            {
                "criteria": ["G6"],
                "decision": "block",
                "priority": 0.7,
                "severity": "major",
                "finding": "b",
            }
        ],
        "t3": [
            {
                "criteria": ["G6"],
                "decision": "advisory",
                "priority": 0.3,
                "severity": "minor",
                "finding": "c",
            }
        ],
        "t4": [
            {
                "criteria": ["G6"],
                "decision": "advisory",
                "priority": 0.4,
                "severity": "minor",
                "finding": "d",
            }
        ],
    }
    m = gei.compute_metrics(rows, fbt)
    # G6: 2 blocking (t1 force-closed, t2 reopened) -> 1.0; 2 advisory (t4 adverse only) -> 0.5.
    assert m["G6"]["blocking_fp_proxy"] == 1.0
    assert m["G6"]["advisory_application_rate"] == 0.5
    assert m["G6"]["sample_counts"] == {
        "blocking_findings": 2,
        "advisory_findings": 2,
        "tickets": 4,
    }
    # mirrors AC2's non-triviality proving command
    assert any(
        isinstance(v.get("blocking_fp_proxy"), (int, float))
        and isinstance(v.get("advisory_application_rate"), (int, float))
        and isinstance(v.get("sample_counts"), dict)
        and sum(v["sample_counts"].values()) > 0
        for v in m.values()
    )


def test_window_yields_subset():
    all_rows = gei.load_corpus(window=None)
    assert len(all_rows) > 5  # the committed corpus is non-empty
    assert gei.load_corpus(window=3) == all_rows[-3:]  # last N in emission order
    assert gei.load_corpus(window=1) == all_rows[-1:]
