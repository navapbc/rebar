"""Golden unit tests for the E6 (ticket a880) pure agreement/permutation helpers.

CI-collectable and LLM-free: exercises ``e6_metrics`` (Fleiss' κ, raw agreement, section
permutation, and the infra-INDETERMINATE exclusion/retry-cap policy) on hand-computed fixtures.
The module lives under ``docs/experiments/plan-review-gate/harnesses/`` and imports ONLY the
standard library, so this test never needs the ``[agents]`` extra or an ``anthropic`` client.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HARNESS_DIR = Path(__file__).resolve().parents[2] / "docs/experiments/plan-review-gate/harnesses"
sys.path.insert(0, str(HARNESS_DIR))

import e6_metrics as M  # noqa: E402 — after the sys.path shim


# ── Fleiss' kappa + raw agreement ─────────────────────────────────────────────────────
def test_perfect_agreement_distinct_categories():
    # Every subject unanimous, but across DISTINCT categories: P_bar == 1, P_e < 1 ⇒ kappa == 1.
    ratings = [
        ["block", "block", "block"],
        ["advisory", "advisory", "advisory"],
        ["dropped", "dropped", "dropped"],
    ]
    assert M.fleiss_kappa(ratings) == pytest.approx(1.0)
    assert M.raw_agreement(ratings) == pytest.approx(1.0)


def test_single_category_trips_zero_variance_guard():
    # All ratings in ONE category ⇒ P_e == 1 ⇒ the chance-corrected form is 0/0. The validity
    # guard returns 1.0 (trivially perfect agreement) instead of dividing by zero.
    ratings = [["advisory", "advisory", "advisory"]] * 4
    assert M.fleiss_kappa(ratings) == pytest.approx(1.0)
    assert M.raw_agreement(ratings) == pytest.approx(1.0)


def test_known_kappa_table():
    # Hand-computed: n=3 raters, N=4 subjects, 2 categories.
    #   s1: A A A -> P1 = 1        s2: B B B -> P2 = 1
    #   s3: A A B -> P3 = 1/3      s4: A B B -> P4 = 1/3
    #   P_bar = 2/3 ; column totals A=6 B=6 of 12 -> p_A=p_B=0.5 -> P_e=0.5
    #   kappa = (2/3 - 1/2) / (1 - 1/2) = 1/3
    ratings = [["A", "A", "A"], ["B", "B", "B"], ["A", "A", "B"], ["A", "B", "B"]]
    assert M.fleiss_kappa(ratings) == pytest.approx(1.0 / 3.0)
    assert M.raw_agreement(ratings) == pytest.approx(0.5)  # only s1, s2 unanimous


def test_kappa_validity_guards():
    with pytest.raises(ValueError):
        M.fleiss_kappa([])  # no subjects
    with pytest.raises(ValueError):
        M.fleiss_kappa([["A", "A", "A"], ["B", "B"]])  # unequal rater counts
    with pytest.raises(ValueError):
        M.fleiss_kappa([["A"]])  # fewer than 2 raters


def test_compute_agreement_pass_fail_against_floors():
    perfect = M.compute_agreement([["PASS"] * 3, ["BLOCK"] * 3, ["PASS"] * 3])
    assert perfect["pass"] is True
    assert perfect["fleiss_kappa"] == pytest.approx(1.0)
    assert perfect["raw_agreement"] == pytest.approx(1.0)
    # A table with heavy disagreement lands below both floors.
    noisy = M.compute_agreement([["A", "B", "C"], ["A", "A", "B"], ["C", "B", "A"]])
    assert noisy["pass"] is False


# ── Section permutation ────────────────────────────────────────────────────────────────
PLAN = (
    "# Title line\n"
    "intro paragraph before any section\n\n"
    "## Alpha\n"
    "alpha body line\n\n"
    "## Beta\n"
    "beta body line\n"
    "### Beta sub\n"
    "sub content stays inside Beta\n\n"
    "## Gamma\n"
    "gamma body line\n"
)
PLAN_ID = "1a2b-c3d4-e5f6-4788"


def test_split_and_count_sections():
    head, blocks = M.split_plan_sections(PLAN)
    assert head.startswith("# Title line")
    assert len(blocks) == 3
    assert M.count_top_sections(PLAN) == 3
    # A '### ' subsection is NOT a top-level split point — it stays inside Beta's block.
    assert "Beta sub" in blocks[1]
    # Round-trip: head + blocks reproduces the plan byte for byte.
    assert head + "".join(blocks) == PLAN


def test_permute_sections_shape_and_identity():
    perms = M.permute_sections(PLAN, PLAN_ID, n_perms=3)
    assert len(perms) == 3
    assert perms[0]["permutation_index"] == 0
    assert perms[0]["section_order"] == [0, 1, 2]  # permutation 0 is the identity
    assert perms[0]["text"] == PLAN  # identity reproduces the original exactly


def test_permute_sections_distinct_and_reproducible():
    perms = M.permute_sections(PLAN, PLAN_ID, n_perms=3)
    orders = [tuple(p["section_order"]) for p in perms]
    assert len(set(orders)) == 3  # all three orderings are mutually distinct
    # The pinned per-plan seed makes the whole selection reproducible.
    again = M.permute_sections(PLAN, PLAN_ID, n_perms=3)
    assert [p["section_order"] for p in perms] == [p["section_order"] for p in again]


def test_permute_sections_preserves_content():
    _, original_blocks = M.split_plan_sections(PLAN)
    for perm in M.permute_sections(PLAN, PLAN_ID, n_perms=3):
        head, blocks = M.split_plan_sections(perm["text"])
        assert head.startswith("# Title line")  # head is never reordered
        assert sorted(blocks) == sorted(original_blocks)  # only the block ORDER changes


def test_permute_sections_rejects_too_few_orderings():
    two_section = "## One\na\n\n## Two\nb\n"
    with pytest.raises(ValueError):
        M.permute_sections(two_section, "0002-0000-0000-0000", n_perms=3)  # 2! == 2 < 3


# ── Infra-INDETERMINATE exclusion + retry cap ─────────────────────────────────────────
def test_infra_vote_classification():
    assert M.is_infra_indeterminate_vote("indeterminate") is True
    for substantive in ("block", "advisory", "dropped"):
        assert M.is_infra_indeterminate_vote(substantive) is False


def test_infra_verdict_classification():
    # A genuine judge-INDETERMINATE (no infra flags) is KEPT, not re-run.
    assert M.is_infra_indeterminate_verdict("INDETERMINATE", {}) is False
    assert M.is_infra_indeterminate_verdict("INDETERMINATE", {"llm_unavailable": True}) is True
    assert M.is_infra_indeterminate_verdict("INDETERMINATE", {"verify_failed": True}) is True
    assert M.is_infra_indeterminate_verdict("PASS", {"llm_unavailable": True}) is False


def test_finalize_votes_reaches_target():
    raw = [
        {"decision": "advisory", "infra": False},
        {"decision": "indeterminate", "infra": True},  # dropped-and-re-run
        {"decision": "advisory", "infra": False},
        {"decision": "block", "infra": False},
    ]
    out = M.finalize_votes(raw, is_infra=lambda a: a["infra"], target=3)
    assert out["excluded"] is False
    assert [v["decision"] for v in out["votes"]] == ["advisory", "advisory", "block"]
    assert out["n_infra"] == 1
    assert out["n_substantive"] == 3


def test_finalize_votes_records_exclusion_never_pads():
    raw = [
        {"decision": "advisory", "infra": False},
        {"decision": "indeterminate", "infra": True},
        {"decision": "indeterminate", "infra": True},
    ]
    out = M.finalize_votes(raw, is_infra=lambda a: a["infra"], target=3)
    assert out["excluded"] is True  # only 1 substantive vote — excluded, NOT padded
    assert out["n_substantive"] == 1
    assert len(out["votes"]) == 1


# ── Exp B: graceful skip of a plan whose ticket vanished from the store (a880) ─────────
# These exercise the driver's non-LLM guard/materialization seam. Importing the driver is
# safe here: its heavy deps (rebar / three_pass / plan_review) are imported lazily INSIDE
# run-a/run-b, so module import needs only the stdlib + e6_metrics (already on sys.path).
import e6_judge_reliability as H  # noqa: E402 — after the HARNESS_DIR sys.path shim


def test_is_ticket_not_found_matches_only_not_found():
    # The exact RebarError text `edit_ticket` raises for an absent ticket is classified as
    # not-found; any OTHER failure is NOT — so the guard never swallows a real bug.
    nf = RuntimeError("rebar edit failed (exit 1): Error: ticket '8722-f153-bd26-46d8' not found")
    assert H._is_ticket_not_found(nf) is True
    for other in (
        RuntimeError("rebar edit failed (exit 1): Error: concurrency conflict"),
        ValueError("some unrelated failure"),
    ):
        assert H._is_ticket_not_found(other) is False


def test_materialize_exp_b_routes_vanished_ticket_to_excluded(tmp_path, monkeypatch):
    # A B_RAW journal mixing a normal kept verdict with a vanished-ticket EXCLUDED marker
    # (which carries infra=False) must route the marker to B_EXCLUDED — never mis-read as a
    # verdict — and keep the healthy plan in B_RESULTS.
    monkeypatch.setattr(H, "B_RAW", tmp_path / "b.raw.jsonl")
    monkeypatch.setattr(H, "B_RESULTS", tmp_path / "b.jsonl")
    monkeypatch.setattr(H, "B_EXCLUDED", tmp_path / "b.excluded.jsonl")
    H._write_jsonl(
        H.B_RAW,
        [
            {
                "plan_id": "aaaa-1111-2222-3333",
                "permutation_index": 0,
                "section_order": [0, 1, 2],
                "verdict": "PASS",
                "fired_criteria": ["G6"],
                "infra": False,
            },
            H._excluded_attempt(
                {
                    "plan_id": "8722-f153-bd26-46d8",
                    "permutation_index": 0,
                    "section_order": [0, 1, 2],
                },
                attempt=0,
                reason="ticket_not_found",
            ),
        ],
    )
    H._materialize_exp_b()
    results = H._read_jsonl(H.B_RESULTS)
    excluded = H._read_jsonl(H.B_EXCLUDED)
    assert [r["plan_id"] for r in results] == ["aaaa-1111-2222-3333"]  # kept, real verdict
    assert len(excluded) == 1
    assert excluded[0]["plan_id"] == "8722-f153-bd26-46d8"
    assert excluded[0]["reason"] == "ticket_not_found"
