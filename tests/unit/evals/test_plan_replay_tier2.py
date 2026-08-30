from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

import rebar
from rebar.config import tracker_dir
from rebar.llm.evals.plan_replay import ledger, tier2
from rebar.llm.plan_review.container_stage import CONTAINER_CRITERIA


def _init_source_tracker() -> str:
    """A source-project stand-in: a git repo with ONE real committed file (so ``HEAD``
    resolves for ``git worktree add``, as :func:`tier2.materialize_reconstructed_ticket`
    needs, and a materialized scratch worktree has something real to assert against) plus
    a mounted ticket tracker."""
    root = tempfile.mkdtemp(prefix="tier2-src-test-")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (Path(root) / "SOURCE_MARKER.txt").write_text("real source file\n", encoding="utf-8")
    subprocess.run(["git", "add", "SOURCE_MARKER.txt"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        cwd=root,
        check=True,
    )
    rebar.init_repo(repo_root=root, force_new_store=True)
    return root


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


# ── reconstructed-material scratch tracker (never review live ticket state) ────────
def test_materialize_reconstructed_ticket_ignores_post_review_drift():
    root = _init_source_tracker()
    created = rebar.create_ticket(
        "task",
        "original title",
        description="ORIGINAL description",
        repo_root=root,
        return_alias=True,
    )
    ticket_id = created["id"]
    review_ts = int(time.time() * 1e9)
    time.sleep(0.05)
    rebar.edit_ticket(
        ticket_id, description="DRIFTED description (post-review edit)", repo_root=root
    )

    tracker_path = str(tracker_dir(root))
    row = {
        "ticket_id": ticket_id,
        "ticket_type": "task",
        "description": "ORIGINAL description",
        "file_impact": [],
        "children": [],
        "review_event_ts": review_ts,
    }
    scratch_root, scratch_id = tier2.materialize_reconstructed_ticket(
        row, tracker_path, ticket_repo_root=root
    )
    try:
        scratch_ticket = rebar.show_ticket(scratch_id, repo_root=scratch_root)
        assert scratch_ticket["description"] == "ORIGINAL description"
        assert scratch_ticket["ticket_type"] == "task"
    finally:
        tier2.cleanup_reconstructed_ticket(scratch_root, ticket_repo_root=root)
        shutil.rmtree(root, ignore_errors=True)


def test_materialize_reconstructed_ticket_scratch_root_has_real_source_files():
    # The whole point of basing the scratch root on a `git worktree` rather than a bare
    # empty repo: code-grounded criteria (G3/G4/T8/etc) read `ctx.repo_root`, which
    # `resolve_code_root`'s explicit-override rule resolves to the SAME `repo_root` the
    # ticket read uses -- an empty scratch dir there silently starves them of evidence.
    root = _init_source_tracker()
    created = rebar.create_ticket("task", "t", description="d", repo_root=root, return_alias=True)
    row = {
        "ticket_id": created["id"],
        "ticket_type": "task",
        "description": "d",
        "file_impact": [],
        "children": [],
        "review_event_ts": int(time.time() * 1e9),
    }
    tracker_path = str(tracker_dir(root))
    scratch_root, _scratch_id = tier2.materialize_reconstructed_ticket(
        row, tracker_path, ticket_repo_root=root
    )
    try:
        assert (Path(scratch_root) / "SOURCE_MARKER.txt").read_text(encoding="utf-8") == (
            "real source file\n"
        )
    finally:
        tier2.cleanup_reconstructed_ticket(scratch_root, ticket_repo_root=root)
        shutil.rmtree(root, ignore_errors=True)


def test_materialize_reconstructed_ticket_reconstructs_direct_children():
    root = _init_source_tracker()
    parent_created = rebar.create_ticket(
        "story", "parent", description="PARENT description", repo_root=root, return_alias=True
    )
    parent_id = parent_created["id"]
    child_created = rebar.create_ticket(
        "task",
        "child",
        description="CHILD description",
        parent=parent_id,
        repo_root=root,
        return_alias=True,
    )
    review_ts = int(time.time() * 1e9)

    tracker_path = str(tracker_dir(root))
    row = {
        "ticket_id": parent_id,
        "ticket_type": "story",
        "description": "PARENT description",
        "file_impact": [],
        "children": [child_created["id"]],
        "review_event_ts": review_ts,
    }
    scratch_root, scratch_id = tier2.materialize_reconstructed_ticket(
        row, tracker_path, ticket_repo_root=root
    )
    try:
        from rebar.llm.plan_review.context_assembly import assemble_context

        ctx = assemble_context(scratch_id, repo_root=scratch_root)
        assert ctx.has_children is True
    finally:
        tier2.cleanup_reconstructed_ticket(scratch_root, ticket_repo_root=root)
        shutil.rmtree(root, ignore_errors=True)


def test_reconstruct_child_returns_none_for_untracked_child():
    assert tier2._reconstruct_child({}, "not-a-real-id", review_ts=0) is None


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


def test_finding_set_comparison_excludes_det_floor_stored_finding():
    # A stored finding tagged ENTIRELY with a DET-floor criterion (P1..P11) can
    # never be reproduced by the candidate (Pass-1-only replay never produces a
    # DET-floor finding) -- it must not count as "lost" or drag Jaccard down.
    stored = [_finding("acceptance criteria quality issue", ["P6"])]
    result = tier2.finding_set_comparison(stored, [])
    assert result["jaccard"] == 1.0
    assert result["per_criterion"] == {}


def test_finding_set_comparison_keeps_non_det_stored_finding_alongside_det():
    stored = [
        _finding("acceptance criteria quality issue", ["P6"]),
        _finding("wombat platypus koala echidna", ["E1"]),
    ]
    result = tier2.finding_set_comparison(stored, [])
    assert result["jaccard"] == 0.0
    assert result["per_criterion"] == {"E1": {"gained": 0, "lost": 1, "unchanged": 0}}


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


def test_candidate_verdict_flip_empty_candidate_returns_empty_sets():
    row = {"sidecar_data": {"findings": [_finding("alpha bravo charlie delta", ["E1"])]}}
    result = tier2.candidate_verdict_flip(
        row, [], run_chunk=lambda *a, **k: [], model_id="bedrock:opus"
    )
    assert result == {"stored_blocking_criteria": set(), "candidate_blocking_criteria": set()}


def test_candidate_verdict_flip_derives_blocking_criteria_from_decisions(monkeypatch):
    stored = [{"decision": "block", "criteria": ["E1"]}, {"decision": "pass", "criteria": ["G6"]}]
    row = {"sidecar_data": {"findings": stored}}
    candidate_findings = [_finding("wombat platypus koala echidna", ["T8"])]

    monkeypatch.setattr(tier2, "execution_review_for", lambda data: False)
    monkeypatch.setattr(
        tier2, "verify_findings", lambda *a, **k: {"verifications": {0: {"binary": "PASS"}}}
    )
    monkeypatch.setattr(
        tier2,
        "live_baseline_decisions",
        lambda findings, verifs, *, execution_review: [
            {**f, "decision": "block"} for f in findings
        ],
    )

    result = tier2.candidate_verdict_flip(
        row, candidate_findings, run_chunk=lambda *a, **k: [], model_id="bedrock:opus"
    )
    assert result == {
        "stored_blocking_criteria": {"E1"},
        "candidate_blocking_criteria": {"T8"},
    }


def test_candidate_verdict_flip_excludes_det_floor_stored_blocking(monkeypatch):
    # A stored DET-floor "block" (P1..P11) can never be reproduced by the
    # candidate replay -- it must not count as a stored-blocking criterion, or it
    # would always read as "relieved" regardless of candidate behavior.
    stored = [
        {"decision": "block", "criteria": ["P6"]},
        {"decision": "block", "criteria": ["E1"]},
    ]
    row = {"sidecar_data": {"findings": stored}}
    candidate_findings = [_finding("wombat platypus koala echidna", ["E1"])]

    monkeypatch.setattr(tier2, "execution_review_for", lambda data: False)
    monkeypatch.setattr(
        tier2, "verify_findings", lambda *a, **k: {"verifications": {0: {"binary": "PASS"}}}
    )
    monkeypatch.setattr(
        tier2,
        "live_baseline_decisions",
        lambda findings, verifs, *, execution_review: [
            {**f, "decision": "block"} for f in findings
        ],
    )

    result = tier2.candidate_verdict_flip(
        row, candidate_findings, run_chunk=lambda *a, **k: [], model_id="bedrock:opus"
    )
    assert result == {
        "stored_blocking_criteria": {"E1"},
        "candidate_blocking_criteria": {"E1"},
    }


# ── budget / cost tier ───────────────────────────────────────────────────────────────
def test_cost_tier_for_criterion_agent_tier_direct():
    from rebar.llm.plan_review import registry

    desc = registry.by_id()["T5c"]
    assert registry.exec_tier(desc) == "AGENT"
    assert tier2.cost_tier_for_criterion(desc) == "AGENT"


def test_cost_tier_for_criterion_non_agent_non_container_stays_its_own_tier():
    from rebar.llm.plan_review import registry

    desc = registry.by_id()["E1"]
    tier = registry.exec_tier(desc)
    assert tier != "AGENT"
    assert desc["id"] not in CONTAINER_CRITERIA
    assert tier2.cost_tier_for_criterion(desc) == tier


def test_cost_tier_for_criterion_container_override_forces_agent(monkeypatch):
    # G3 is a real CONTAINER_CRITERIA member; force exec_tier to answer non-AGENT to
    # prove the override clause fires independently of exec_tier's own answer.
    monkeypatch.setattr("rebar.llm.plan_review.registry.exec_tier", lambda desc: "1-TURN")
    assert tier2.cost_tier_for_criterion({"id": "G3"}) == "AGENT"


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


def test_run_tier2_single_criterion_raises_on_unknown_criterion_id(tmp_path, monkeypatch):
    def _pinned(pass_name, *, repo_root=None):
        from rebar.llm.evals.plan_replay.parity import PinnedModel

        return PinnedModel(model_id="bedrock:opus", config_root=str(tmp_path))

    monkeypatch.setattr(tier2.parity, "resolve_pinned_model", _pinned)
    monkeypatch.setattr(tier2, "build_sampling_pool", lambda *a, **k: [_row("t1", "a")])
    monkeypatch.setattr(
        tier2,
        "load_sample_file",
        lambda *a, **k: [{"store": "rebar", "ticket_id": "t1", "review_event_uuid": "uuid-a"}],
    )
    monkeypatch.setattr(tier2, "resolve_sample_against_pool", lambda entries, pool: pool)

    def _boom_candidate_pass1(*a, **k):
        raise AssertionError("must not dispatch a run for an unknown criterion id")

    monkeypatch.setattr(tier2, "run_candidate_pass1", _boom_candidate_pass1)

    with pytest.raises(ValueError, match="unknown criterion id"):
        tier2.run_tier2_single_criterion(
            {"rebar": "unused"},
            cache_dir=tmp_path,
            sample_path=str(tmp_path / "sample.json"),
            criterion_id="NOT-A-REAL-ID",
            ticket_repo_root=str(tmp_path),
            ledger_path=str(tmp_path / "ledger.jsonl"),
        )


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
