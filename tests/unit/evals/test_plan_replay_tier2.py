from __future__ import annotations

import json

import pytest

from rebar.llm.evals.plan_replay import ledger, tier2
from rebar.llm.plan_review.container_stage import CONTAINER_CRITERIA


# ── sample entries / resolution ─────────────────────────────────────────────────────
def _row(ticket_id: str, uuid_suffix: str, **overrides) -> dict:
    base = {
        "store": "rebar",
        "ticket_id": ticket_id,
        "review_event_uuid": f"uuid-{uuid_suffix}",
        "verified": True,
        "ran_model": "bedrock:opus",
        "children": False,
        "finding_count": 2,
        "sidecar_data": {"findings": []},
    }
    base.update(overrides)
    return base


def test_sample_entries_round_trips_the_identity_triple():
    sample = [_row("t1", "a"), _row("t2", "b")]
    entries = tier2.sample_entries(sample)
    assert entries == [
        {"store": "rebar", "ticket_id": "t1", "review_event_uuid": "uuid-a"},
        {"store": "rebar", "ticket_id": "t2", "review_event_uuid": "uuid-b"},
    ]


def test_resolve_sample_against_pool_matches_by_identity_triple():
    pool = [_row("t1", "a"), _row("t2", "b")]
    entries = [{"store": "rebar", "ticket_id": "t1", "review_event_uuid": "uuid-a"}]
    resolved = tier2.resolve_sample_against_pool(entries, pool)
    assert resolved == [pool[0]]


def test_resolve_sample_against_pool_raises_on_missing_entry():
    pool = [_row("t1", "a")]
    entries = [{"store": "rebar", "ticket_id": "t2", "review_event_uuid": "uuid-missing"}]
    with pytest.raises(ValueError, match="no longer resolvable"):
        tier2.resolve_sample_against_pool(entries, pool)


def test_load_sample_file_round_trips(tmp_path):
    entries = [{"store": "rebar", "ticket_id": "t1", "review_event_uuid": "u1"}]
    path = tmp_path / "sample.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    assert tier2.load_sample_file(path) == entries


def test_strata_summary_counts_containers_heavy_and_stores():
    sample = [
        _row("t1", "a", children=True, finding_count=12),
        _row("t2", "b", children=False, finding_count=3, store="lmn"),
        _row("t3", "c", children=False, finding_count=15),
    ]
    summary = tier2.strata_summary(sample)
    assert summary["containers"] == 1
    assert summary["heavy"] == 2
    assert summary["store:rebar"] == 2
    assert summary["store:lmn"] == 1


# ── criteria resolution / dispatch shape ────────────────────────────────────────────
def test_single_criterion_request_criteria_shape():
    assert tier2.single_criterion_request_criteria("T5c") == ({"prompt": "plan-review-T5c"},)


def test_resolve_criteria_tiers_splits_container_out_of_agent():
    criteria = tuple({"prompt": f"plan-review-{cid}"} for cid in ("E1", "T5c", "G3"))
    single, agent, container, skipped = tier2.resolve_criteria_tiers(criteria)
    single_ids = {c["id"] for c in single}
    agent_ids = {c["id"] for c in agent}
    container_ids = {c["id"] for c in container}
    assert "E1" in single_ids
    assert "T5c" in agent_ids
    assert "G3" in container_ids
    assert "G3" not in agent_ids
    assert not skipped


def test_container_criteria_constant_matches_registry_contract():
    # G3/G4 must resolve to AGENT from exec_tier, never a "CONTAINER" value -- this
    # constant is what tier2 uses to pull them back out for reporting.
    assert "G3" in CONTAINER_CRITERIA
    assert "G4" in CONTAINER_CRITERIA


# ── finding-set comparison (Jaccard + per-criterion gained/lost) ───────────────────
def _finding(text: str, criteria: list[str]) -> dict:
    return {"finding": text, "criteria": criteria, "evidence": [], "impact": ""}


def test_finding_set_comparison_identical_sets_have_jaccard_one():
    findings = [_finding("alpha bravo charlie delta", ["E1"])]
    result = tier2.finding_set_comparison(findings, findings)
    assert result["jaccard"] == 1.0
    assert result["per_criterion"]["E1"] == {"gained": 0, "lost": 0, "unchanged": 1}


def test_finding_set_comparison_disjoint_sets_have_jaccard_zero():
    stored = [_finding("alpha bravo charlie delta", ["E1"])]
    candidate = [_finding("wombat platypus koala echidna", ["E1"])]
    result = tier2.finding_set_comparison(stored, candidate)
    assert result["jaccard"] == 0.0
    assert result["per_criterion"]["E1"] == {"gained": 1, "lost": 1, "unchanged": 0}


def test_finding_set_comparison_empty_both_is_jaccard_one():
    result = tier2.finding_set_comparison([], [])
    assert result["jaccard"] == 1.0
    assert result["per_criterion"] == {}


def test_finding_set_comparison_multi_criteria_finding_counts_in_each():
    findings = [_finding("alpha bravo charlie delta", ["E1", "G6"])]
    result = tier2.finding_set_comparison(findings, findings)
    assert result["per_criterion"]["E1"]["unchanged"] == 1
    assert result["per_criterion"]["G6"]["unchanged"] == 1


# ── verdict flip aggregation ─────────────────────────────────────────────────────────
def test_verdict_flip_matrix_counts_newly_blocking_and_relieved():
    flips = [
        {"stored_blocking_criteria": {"E1"}, "candidate_blocking_criteria": {"E1", "G6"}},
        {"stored_blocking_criteria": {"T8"}, "candidate_blocking_criteria": set()},
    ]
    matrix = tier2.verdict_flip_matrix(flips)
    assert matrix["newly_blocking"] == 1  # G6 gained on row 1
    assert matrix["relieved"] == 1  # T8 lost on row 2


def test_verdict_flip_matrix_empty_is_zero():
    assert tier2.verdict_flip_matrix([]) == {"newly_blocking": 0, "relieved": 0}


# ── budget pre-flight ────────────────────────────────────────────────────────────────
def test_run_tier2_full_refuses_before_any_pass1_call_when_estimate_exceeds_budget(
    tmp_path, monkeypatch
):
    ledger_path = str(tmp_path / "ledger.jsonl")
    # Pre-fill the ledger near the cap so any estimate is refused.
    with open(ledger_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"usd": ledger.LEDGER_CAP_USD - ledger.LEDGER_RESERVE_USD}) + "\n")

    called = []

    def _boom_resolve_pinned_model(pass_name, *, repo_root=None):
        from rebar.llm.evals.plan_replay.parity import PinnedModel

        return PinnedModel(model_id="bedrock:opus", config_root=str(tmp_path))

    def _boom_pool(*a, **k):
        called.append("pool")
        return [_row("t1", "a")]

    def _boom_load_sample(*a, **k):
        return [{"store": "rebar", "ticket_id": "t1", "review_event_uuid": "uuid-a"}]

    def _boom_candidate_pass1(*a, **k):
        called.append("pass1")
        raise AssertionError("must not be called once budget is refused")

    monkeypatch.setattr(tier2.parity, "resolve_pinned_model", _boom_resolve_pinned_model)
    monkeypatch.setattr(tier2, "build_sampling_pool", _boom_pool)
    monkeypatch.setattr(tier2, "load_sample_file", _boom_load_sample)
    monkeypatch.setattr(tier2, "resolve_sample_against_pool", lambda entries, pool: pool)
    monkeypatch.setattr(tier2, "run_candidate_pass1", _boom_candidate_pass1)

    with pytest.raises(ledger.BudgetExceeded):
        tier2.run_tier2_full(
            {"rebar": "unused"},
            cache_dir=tmp_path,
            sample_path=tmp_path / "sample.json",
            candidate_dir=None,
            candidate_name="current",
            ticket_repo_root=str(tmp_path),
            ledger_path=ledger_path,
        )
    assert "pass1" not in called


def test_run_tier2_full_raises_on_empty_resolved_sample(tmp_path, monkeypatch):
    from rebar.llm.evals.plan_replay.parity import PinnedModel

    monkeypatch.setattr(
        tier2.parity,
        "resolve_pinned_model",
        lambda pass_name, **k: PinnedModel(model_id="bedrock:opus", config_root=str(tmp_path)),
    )
    monkeypatch.setattr(tier2, "build_sampling_pool", lambda *a, **k: [])
    monkeypatch.setattr(tier2, "load_sample_file", lambda *a, **k: [])
    monkeypatch.setattr(tier2, "resolve_sample_against_pool", lambda entries, pool: [])

    with pytest.raises(ValueError, match="zero rows"):
        tier2.run_tier2_full(
            {"rebar": "unused"},
            cache_dir=tmp_path,
            sample_path=tmp_path / "sample.json",
            candidate_dir=None,
            candidate_name="current",
            ticket_repo_root=str(tmp_path),
            ledger_path=str(tmp_path / "ledger.jsonl"),
        )


# ── candidate override installation ─────────────────────────────────────────────────
def test_install_candidate_override_none_leaves_config_root_untouched(tmp_path):
    config_root = tmp_path / "config"
    config_root.mkdir()
    tier2.install_candidate_override(None, str(config_root))
    assert not (config_root / ".rebar").exists()


def test_install_candidate_override_copies_dot_rebar_subtree(tmp_path):
    candidate_dir = tmp_path / "candidate"
    (candidate_dir / ".rebar" / "prompts").mkdir(parents=True)
    (candidate_dir / ".rebar" / "prompts" / "plan_review_E1.md").write_text(
        "override", encoding="utf-8"
    )
    config_root = tmp_path / "config"
    config_root.mkdir()

    tier2.install_candidate_override(str(candidate_dir), str(config_root))

    installed = config_root / ".rebar" / "prompts" / "plan_review_E1.md"
    assert installed.read_text(encoding="utf-8") == "override"


def test_install_candidate_override_raises_when_dot_rebar_missing(tmp_path):
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    config_root = tmp_path / "config"
    config_root.mkdir()
    with pytest.raises(FileNotFoundError, match="no \\.rebar"):
        tier2.install_candidate_override(str(candidate_dir), str(config_root))


# ── report rendering ─────────────────────────────────────────────────────────────────
def test_render_tier2_report_full_mode():
    result = {
        "run_id": "tier2-current-abc123",
        "mode": "full",
        "candidate": "current",
        "model_id": "bedrock:opus",
        "sample_n": 20,
        "jaccard_mean": 0.85,
        "verdict_flip": {"newly_blocking": 1, "relieved": 0},
        "ledger_entry": {"usd": 12.34},
    }
    text = tier2.render_tier2_report(result)
    assert "tier2-current-abc123" in text
    assert "Candidate: `current`" in text
    assert "$12.34" in text
    assert "newly_blocking=1" in text


def test_render_tier2_report_single_criterion_mode():
    result = {
        "run_id": "tier2-T5c-abc123",
        "mode": "single-criterion",
        "criterion_id": "T5c",
        "exec_tier": "AGENT",
        "model_id": "bedrock:opus",
        "sample_n": 20,
        "jaccard_mean": 0.9,
        "ledger_entry": {"usd": 4.5},
    }
    text = tier2.render_tier2_report(result)
    assert "Criterion: `T5c` (exec_tier=AGENT)" in text
