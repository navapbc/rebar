"""Held-out oracle for the plan-review fixture selector (ticket 549b).

The IMPLEMENTER MUST NOT SEE THIS FILE. It is relocated out of the working tree while the
implementation subagent works and restored by the orchestrator for validation. It exercises
the cases that separate a real selector from one that fakes the happy path: the vintage gate
(AC1/AC4), zero-candidate rows (AC3), rubric path resolution (AC2), the tiered bar
(AC5), no-fire admission (AC6), escaped-defect priority (AC7), the signal set (AC8), rank
order (AC9), and the E1 advisory (an eligible criterion with no admitted candidate).
"""

from __future__ import annotations

import pytest
from test_fixture_selection import finding, review

from rebar.llm.criteria.ids import criterion_prompt_id
from rebar.llm.evals.fixture_selection import (
    MIN_MARGIN,
    rubric_path,
    select_candidates,
)
from rebar.llm.prompting.prompts import get_prompt


def _candidates(rows):
    return [r for r in rows if r["kind"] == "candidate"]


def _zeros(rows):
    return [r for r in rows if r["kind"] == "zero_candidate"]


# --- Regression (change 2552 LLM-Review): default vintage gate must compare in nanoseconds --


def test_default_vintage_gate_compares_in_nanoseconds():
    """select_candidates' DEFAULT vintage path (``rubric_history=None``) resolves the rubric
    timestamp via ``last_rubric_commit_ts``, which returns epoch SECONDS — but
    ``review_event_ts`` is epoch NANOSECONDS. The default path must convert to ns before
    comparing, or every corpus review dwarfs the raw-second rubric ts and the gate never
    excludes. A review whose ns timestamp PREDATES the T2 rubric commit (in ns) must be
    excluded even though that ns value far exceeds the rubric's epoch-second value."""
    from pathlib import Path

    repo_root = str(Path(__file__).resolve().parents[2])
    C = "T2"
    # 1e18 ns predates the T2 rubric commit (~1.782e18 ns) yet exceeds it as seconds (~1.782e9)
    r = review(
        "pre",
        1_000_000_000_000_000_000,
        "A",
        [finding("n1", criteria=[C], decision_margin=0.20)],
    )
    rows = select_candidates([r], criteria_ids=[C], repo_root=repo_root, base_ref="HEAD")
    assert _candidates(rows) == []
    assert [z["reason"] for z in _zeros(rows)] == ["no-admitted-candidate"]


# --- Regression (change 2552 LLM-Review): vintage git-logs the IN-REPO rubric path ---------


def test_vintage_git_logs_in_repo_rubric_not_installed_catalog(tmp_path):
    """The vintage gate git-logs history in ``repo_root``. When rebar is installed as a wheel,
    the packaged catalog dir lives OUTSIDE ``repo_root``, so git-logging the installed catalog
    path finds no history and every packaged-rubric criterion silently collapses to
    'no-committed-prompt-history'. The gate must instead resolve the rubric's canonical in-repo
    source path (``<repo_root>/src/rebar/llm/reviewers/<fallback_file>``), which git tracks, so
    a committed rubric in ``repo_root`` yields its real commit timestamp — not ``None``."""
    import subprocess

    from rebar.llm.evals.fixture_selection import last_rubric_commit_ts

    C = "T2"
    pid = criterion_prompt_id(C, gate_key="plan_review")
    fallback = get_prompt(pid, repo_root=str(tmp_path)).fallback_file
    # A standalone git repo, distinct from the installed catalog dir (the wheel scenario).
    rubric = tmp_path / "src" / "rebar" / "llm" / "reviewers" / fallback
    rubric.parent.mkdir(parents=True)
    rubric.write_text("rubric body\n", encoding="utf-8")

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    _git("init", "-q")
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")
    _git("add", "-A")
    _git("commit", "-qm", "add rubric")
    expected = int(_git("log", "-1", "--format=%ct", "HEAD").stdout.strip())

    got = last_rubric_commit_ts(C, repo_root=str(tmp_path), base_ref="HEAD")
    assert got == expected


# --- AC1: vintage gate excludes a review predating its criterion's rubric commit ----------


def test_vintage_excludes_earlier_review_keeps_later():
    C = "project.alpha"
    reviews = [
        review("early", 999, "A", [finding("n1", criteria=[C], decision_margin=0.20)]),
        review("late", 1001, "B", [finding("n1", criteria=[C], decision_margin=0.20)]),
    ]
    rows = select_candidates(reviews, criteria_ids=[C], rubric_history=lambda c: 1000)
    cands = _candidates(rows)
    assert len(cands) == 1
    assert cands[0]["review_event_uuid"] == "late"


# --- AC4: a fire candidate failing the vintage gate yields no candidate row ---------------


def test_fire_failing_vintage_yields_no_candidate_row():
    C = "project.alpha"
    reviews = [
        review("early", 999, "A", [finding("n1", criteria=[C], decision_margin=0.20)]),
    ]
    rows = select_candidates(reviews, criteria_ids=[C], rubric_history=lambda c: 1000)
    assert _candidates(rows) == []
    # eligible criterion, no admitted candidate -> the E1 zero row, NOT an advisory one
    assert [z["reason"] for z in _zeros(rows)] == ["no-admitted-candidate"]


# --- AC3: excluded criteria carry a zero-candidate row naming the reason -------------------


@pytest.mark.parametrize(
    ("rubric_history", "unreliable", "expected_reason"),
    [
        (lambda c: None, None, "no-committed-prompt-history"),
        (lambda c: 1000, {"project.alpha": "REB-999"}, "unreliable-criterion:REB-999"),
    ],
)
def test_excluded_criterion_zero_row_names_reason(rubric_history, unreliable, expected_reason):
    C = "project.alpha"
    reviews = [review("u", 1001, "A", [finding("n1", criteria=[C], decision_margin=0.20)])]
    rows = select_candidates(
        reviews, criteria_ids=[C], rubric_history=rubric_history, unreliable=unreliable
    )
    assert _candidates(rows) == []
    zeros = _zeros(rows)
    assert len(zeros) == 1
    assert zeros[0]["criterion"] == C
    assert zeros[0]["reason"] == expected_reason


# --- AC2: rubric path resolution (override-wins, else packaged fallback) -------------------


def test_rubric_path_prefers_repo_override(tmp_path):
    C = "T2"
    pid = criterion_prompt_id(C, gate_key="plan_review")
    override = tmp_path / ".rebar" / "prompts" / f"{pid}.md"
    override.parent.mkdir(parents=True)
    override.write_text("custom rubric", encoding="utf-8")
    assert rubric_path(C, repo_root=str(tmp_path)) == override


def test_rubric_path_falls_back_to_packaged(tmp_path):
    C = "T2"
    pid = criterion_prompt_id(C, gate_key="plan_review")
    prompt = get_prompt(pid, repo_root=str(tmp_path))
    assert prompt.fallback_file  # this criterion is packaged
    resolved = rubric_path(C, repo_root=str(tmp_path))
    # no override present -> a packaged reviewers/<fallback_file> path that exists on disk
    assert resolved.name == prompt.fallback_file.split("/")[-1]
    assert resolved.is_file()


# --- AC5: tiered bar (all three signals -> blocking; remove any -> advisory) ---------------


def _fire_reviews(*, consensus: bool, author_response: bool, margin):
    """Build reviews yielding a single fire candidate for project.alpha with the requested
    tier signals. Consensus needs two equal-fingerprint fires; author_response needs the key
    to drop across a consecutive differing-material pair; margin sets decision_margin."""
    C = "project.alpha"

    def f():
        return finding("n1", criteria=[C], decision_margin=margin)

    reviews = [review("u1", 1001, "A", [f()])]
    if consensus:
        reviews.append(review("u2", 1002, "A", [f()]))
    if author_response:
        # a later differing-material review where n1 is absent -> resolved_by_author
        reviews.append(review("u3", 1003, "B", []))
    else:
        # a later differing-material review where n1 persists -> not resolved
        reviews.append(review("u3", 1003, "B", [f()]))
    return reviews


def test_all_three_signals_yield_blocking():
    rows = select_candidates(
        _fire_reviews(consensus=True, author_response=True, margin=0.20),
        criteria_ids=["project.alpha"],
        rubric_history=lambda c: 1000,
    )
    cands = _candidates(rows)
    assert len(cands) == 1
    assert cands[0]["tier"] == "blocking"


@pytest.mark.parametrize(
    ("consensus", "author_response", "margin"),
    [
        (False, True, 0.20),  # no reproduction consensus
        (True, False, 0.20),  # no author response
        (True, True, 0.10),  # margin below MIN_MARGIN
    ],
)
def test_removing_any_signal_downgrades_to_advisory(consensus, author_response, margin):
    rows = select_candidates(
        _fire_reviews(consensus=consensus, author_response=author_response, margin=margin),
        criteria_ids=["project.alpha"],
        rubric_history=lambda c: 1000,
    )
    cands = _candidates(rows)
    assert len(cands) == 1
    assert cands[0]["tier"] == "advisory"


def test_none_margin_never_blocking():
    rows = select_candidates(
        _fire_reviews(consensus=True, author_response=True, margin=None),
        criteria_ids=["project.alpha"],
        rubric_history=lambda c: 1000,
    )
    cands = _candidates(rows)
    assert len(cands) == 1
    assert cands[0]["tier"] == "advisory"
    assert cands[0]["abs_margin"] is None
    assert "margin" not in cands[0]["signals"]


def test_min_margin_floor_is_fifteen_hundredths():
    assert MIN_MARGIN == 0.15


# --- AC6: no-fire admission (routed + uncited; missing-cohort skipped; cited absent) -------


def test_no_fire_admitted_only_when_routed_and_uncited():
    C = "project.alpha"
    other = "project.beta"

    # C is in the cohort of a finding that cites `other` (C routed) and no finding cites C.
    # Two equal-fingerprint reviews keep C silent -> absence reproduction consensus.
    def routed_silent():
        return finding("m1", criteria=[other], cohort=[C, other])

    reviews = [
        review("u1", 1001, "A", [routed_silent()]),
        review("u2", 1002, "A", [routed_silent()]),
    ]
    rows = select_candidates(reviews, criteria_ids=[C], rubric_history=lambda c: 1000)
    cands = _candidates(rows)
    assert len(cands) == 1
    assert cands[0]["direction"] == "no_fire"
    assert cands[0]["norm_id"] is None
    assert cands[0]["tier"] == "advisory"


def test_missing_cohort_is_skipped_not_admitted():
    C = "project.alpha"
    other = "project.beta"

    # finding cites `other`, cohort MISSING (None) -> C is not proven routed -> no no-fire row
    def missing():
        return finding("m1", criteria=[other], cohort=None)

    reviews = [
        review("u1", 1001, "A", [missing()]),
        review("u2", 1002, "A", [missing()]),
    ]
    rows = select_candidates(reviews, criteria_ids=[C], rubric_history=lambda c: 1000)
    assert _candidates(rows) == []


def test_cited_criterion_is_not_a_no_fire_candidate():
    C = "project.alpha"
    # C is cited -> it is a fire (or nothing), never a no-fire candidate
    reviews = [
        review("u1", 1001, "A", [finding("n1", criteria=[C], cohort=[C], decision_margin=0.20)]),
        review("u2", 1002, "A", [finding("n1", criteria=[C], cohort=[C], decision_margin=0.20)]),
    ]
    rows = select_candidates(reviews, criteria_ids=[C], rubric_history=lambda c: 1000)
    no_fire = [c for c in _candidates(rows) if c["direction"] == "no_fire"]
    assert no_fire == []


# --- Regression (change 2552 LLM-Review advisory): one criterion, BOTH fire and no_fire ----


def test_single_criterion_emits_both_fire_and_no_fire_each_ranked_from_zero():
    """A criterion can FIRE in one review (a finding cites it) AND be silently ROUTED in other
    reviews (a finding's ``cohort`` lists it, uncited, reproduced) — the two directions are
    concatenated ``fire + no_fire`` and each direction is ranked independently from 0. Assert
    both appear for the same criterion, in fire-before-no_fire order, each at ``rank`` 0."""
    C = "project.alpha"
    other = "project.beta"

    def routed_silent():
        return finding("m1", criteria=[other], cohort=[C, other])

    reviews = [
        review("f1", 1001, "F", [finding("n1", criteria=[C], decision_margin=0.20)]),
        review("s1", 1002, "S", [routed_silent()]),
        review("s2", 1003, "S", [routed_silent()]),
    ]
    rows = select_candidates(reviews, criteria_ids=[C], rubric_history=lambda c: 1000)
    cands = _candidates(rows)
    assert [c["direction"] for c in cands] == ["fire", "no_fire"]
    assert all(c["criterion"] == C for c in cands)
    fire, no_fire = cands
    assert fire["norm_id"] == "n1"
    assert fire["rank"] == 0
    assert no_fire["norm_id"] is None
    assert no_fire["rank"] == 0
    assert _zeros(rows) == []


# --- AC7: escaped_defect is a priority signal, not a label --------------------------------


def test_escaped_defect_only_signal_admits_nothing():
    C = "project.alpha"
    escaped = {"close_class": "plan_defect"}
    # a single fire with no consensus/author-response/margin, but the ticket escaped a defect
    reviews = [
        review(
            "u1",
            1001,
            "A",
            [finding("n1", criteria=[C], decision_margin=None)],
            ticket_state=escaped,
        ),
    ]
    rows = select_candidates(reviews, criteria_ids=[C], rubric_history=lambda c: 1000)
    assert _candidates(rows) == []


def test_escaped_defect_outranks_equal_signal_peer():
    C = "project.alpha"
    escaped = {"close_class": "plan_defect"}
    # two advisory fire candidates (margin only), same criterion/direction, distinct norm_ids;
    # the one whose review escaped a defect ranks first.
    reviews = [
        review("plain", 1001, "A", [finding("n_plain", criteria=[C], decision_margin=0.20)]),
        review(
            "esc",
            1002,
            "B",
            [finding("n_esc", criteria=[C], decision_margin=0.20)],
            ticket_state=escaped,
        ),
    ]
    rows = select_candidates(reviews, criteria_ids=[C], rubric_history=lambda c: 1000)
    cands = sorted(_candidates(rows), key=lambda r: r["rank"])
    assert [c["norm_id"] for c in cands] == ["n_esc", "n_plain"]
    assert cands[0]["escaped_defect"] is True
    assert cands[0]["rank"] == 0


# --- AC8: signal set for a key seen only in equal-fingerprint pairs ------------------------


def test_equal_fingerprint_only_key_records_consensus_not_author_response():
    C = "project.alpha"

    def f():
        return finding("n1", criteria=[C], decision_margin=None)

    reviews = [
        review("u1", 1001, "A", [f()]),
        review("u2", 1002, "A", [f()]),  # equal fingerprint -> consensus, never a differing pair
    ]
    rows = select_candidates(reviews, criteria_ids=[C], rubric_history=lambda c: 1000)
    cands = _candidates(rows)
    assert len(cands) == 1
    assert cands[0]["signals"] == ["reproduction_consensus"]


# --- AC9: rank order over distinct margins with a None margin last -------------------------


def test_rank_order_descending_margin_none_last():
    C = "project.alpha"
    margins = [0.20, 0.30, 0.40, 0.50, 0.60, 0.70]
    reviews = []
    ts = 1001
    for i, m in enumerate(margins):
        reviews.append(
            review(f"u{i}", ts, f"fp{i}", [finding(f"n{i}", criteria=[C], decision_margin=m)])
        )
        ts += 1
    # a 7th candidate with a None margin, admitted via reproduction consensus (two equal-fp fires)
    reviews.append(review("un_a", ts, "fpN", [finding("nN", criteria=[C], decision_margin=None)]))
    ts += 1
    reviews.append(review("un_b", ts, "fpN", [finding("nN", criteria=[C], decision_margin=None)]))
    rows = select_candidates(reviews, criteria_ids=[C], rubric_history=lambda c: 1000)
    fire = sorted(
        [c for c in _candidates(rows) if c["direction"] == "fire"], key=lambda r: r["rank"]
    )
    assert [c["abs_margin"] for c in fire] == [0.70, 0.60, 0.50, 0.40, 0.30, 0.20, None]
    assert [c["rank"] for c in fire] == list(range(7))


# --- E1 (deferred advisory): eligible, non-skipped, but no admitted candidate -------------


def test_eligible_criterion_with_no_admitted_candidate_gets_distinct_zero_reason():
    C = "project.alpha"
    # eligible (committed history), not skipped, but the lone fire has zero signals
    reviews = [review("u1", 1001, "A", [finding("n1", criteria=[C], decision_margin=None)])]
    rows = select_candidates(reviews, criteria_ids=[C], rubric_history=lambda c: 1000)
    assert _candidates(rows) == []
    zeros = _zeros(rows)
    assert len(zeros) == 1
    assert zeros[0]["reason"] == "no-admitted-candidate"
    assert zeros[0]["reason"] != "no-committed-prompt-history"
