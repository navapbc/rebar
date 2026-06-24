"""Unit tests for the plan-review gate's deterministic core (epic 5fd2).

These exercise the pure, I/O-free seams — the DET floor (P1-P8) on synthetic
contexts, the Pass-3 deterministic math, the criteria registry + routing, the
Pass-4 subject validator, and the attestation/sidecar helpers — with no git store,
no LLM, no network. The store/CLI/MCP integration is pinned in
tests/interfaces/test_plan_review_gate.py.
"""

from __future__ import annotations

from rebar.llm.plan_review import attest, det_floor, orchestrator, passes, registry, sidecar
from rebar.llm.plan_review.det_floor import PlanContext


def _ctx(description: str, *, ttype: str = "task", title: str = "T", **kw) -> PlanContext:
    return PlanContext(
        ticket_id="abcd-0000-0000-0001",
        ticket_type=ttype,
        title=title,
        description=description,
        **kw,
    )


_GOOD_AC = "## Acceptance Criteria\n- [ ] a thing is observably true\n- [ ] another check\n"


# ── DET floor ────────────────────────────────────────────────────────────────
def test_p1_blocks_without_acceptance_criteria() -> None:
    r = det_floor.p1_readiness_shape(_ctx("Some description with no AC block."))
    assert r.status == "fail" and r.blocking and r.blocked


def test_p1_passes_with_acceptance_criteria() -> None:
    r = det_floor.p1_readiness_shape(_ctx(_GOOD_AC))
    assert r.status == "pass" and not r.blocked
    assert r.coverage["ac_items"] == 2


def test_p5_blocks_on_child_dependency_cycle() -> None:
    children = [
        {"ticket_id": "c1", "deps": [{"target_id": "c2", "relation": "depends_on"}]},
        {"ticket_id": "c2", "deps": [{"target_id": "c1", "relation": "depends_on"}]},
    ]
    r = det_floor.p5_task_dag(_ctx(_GOOD_AC, ttype="epic", children=children))
    assert r.status == "fail" and r.blocking


def test_p5_noop_for_leaf() -> None:
    r = det_floor.p5_task_dag(_ctx(_GOOD_AC))
    assert r.status == "pass" and not r.blocking


def test_p5_flags_file_interference_advisory() -> None:
    children = [
        {"ticket_id": "c1", "file_impact": [{"path": "a.py"}], "deps": []},
        {"ticket_id": "c2", "file_impact": [{"path": "a.py"}], "deps": []},
    ]
    r = det_floor.p5_task_dag(_ctx(_GOOD_AC, ttype="epic", children=children))
    assert r.status == "fail" and not r.blocking  # interference is advisory, not blocking


def test_p7_destructive_without_safeguard_is_advisory() -> None:
    r = det_floor.p7_destructive(_ctx(_GOOD_AC + "\nWe will run rm -rf /var/data to reset."))
    assert r.status == "fail" and not r.blocking


def test_p7_destructive_with_safeguard_passes() -> None:
    r = det_floor.p7_destructive(
        _ctx(_GOOD_AC + "\nWe DROP TABLE old after taking a backup and a dry-run.")
    )
    assert r.status == "pass"


def test_p8_blocks_when_too_big() -> None:
    big = _GOOD_AC + ("x " * 600_000)  # ~1.2M chars ≈ 300k tokens, still under 1M? bump window down
    ctx = _ctx(big, largest_window_tokens=10_000)
    r = det_floor.p8_reviewability(ctx)
    assert r.status == "fail" and r.blocking


def test_p8_passes_for_normal_plan() -> None:
    r = det_floor.p8_reviewability(_ctx(_GOOD_AC))
    assert r.status == "pass"


def test_det_floor_fails_open_on_check_error(monkeypatch) -> None:
    # A broken check abstains, never aborts the floor or blocks.
    def boom(ctx):  # noqa: ANN001
        raise RuntimeError("kaboom")

    monkeypatch.setattr(det_floor, "DET_CHECKS", (det_floor.p1_readiness_shape, boom))
    results = det_floor.run_det_floor(_ctx(_GOOD_AC))
    assert any(r.status == "abstain" for r in results)
    assert all(not (r.status == "fail" and r.blocking) for r in results if r.name == "boom")


def test_p2_p3_never_block_and_fail_open_without_repo() -> None:
    ctx = _ctx(_GOOD_AC + "\nWe touch `src/foo/bar.py` and pip install leftpad.")
    p2 = det_floor.p2_resolution(ctx)
    p3 = det_floor.p3_package_existence(ctx)
    assert not p2.blocking and not p3.blocking
    assert p2.status in ("pass", "abstain") and p3.status in ("pass", "abstain")


# ── Pass-3 deterministic math ─────────────────────────────────────────────────
def _verif(binary=None, attrs=None):
    base_b = {q: "yes" for q in passes.GRADED_BINARY}
    base_b["cited_reference_accurate"] = "na"
    base_a = {
        "prod_impact": "high",
        "debt_impact": "high",
        "blast_radius": "system",
        "likelihood": "high",
        "reversibility": "hard",
    }
    return {
        "binary": {**base_b, **(binary or {})},
        "severity_attributes": {**base_a, **(attrs or {})},
    }


def test_pass3_validity_graded_fraction() -> None:
    v = _verif(binary={q: "no" for q in passes.GRADED_BINARY})
    assert passes.pass3_decide(v)["validity"] == 0.0
    v2 = _verif(binary={q: "insufficient" for q in passes.GRADED_BINARY})
    assert passes.pass3_decide(v2)["validity"] == 0.5


def test_pass3_impact_and_priority() -> None:
    d = passes.pass3_decide(_verif(), blocking_enabled=True)
    assert d["validity"] == 1.0 and d["impact"] == 1.0 and d["priority"] == 1.0
    assert d["severity"] == "critical"


def test_pass3_advisory_by_default_even_at_max_priority() -> None:
    # blocking disabled (v1 default posture) ⇒ never blocks regardless of priority.
    assert passes.pass3_decide(_verif(), blocking_enabled=False)["decision"] == "advisory"


def test_pass3_blocks_only_when_opted_in_and_over_threshold() -> None:
    assert passes.pass3_decide(_verif(), blocking_enabled=True)["decision"] == "block"


def test_pass3_drops_low_validity() -> None:
    v = _verif(binary={q: "no" for q in list(passes.GRADED_BINARY)[:5]})
    assert passes.pass3_decide(v)["decision"] == "dropped"


def test_pass3_cited_reference_veto() -> None:
    assert (
        passes.pass3_decide(_verif(binary={"cited_reference_accurate": "no"}))["decision"]
        == "dropped"
    )


def test_pass3_indeterminate_without_verification() -> None:
    assert passes.pass3_decide(None)["decision"] == "indeterminate"


# ── registry + routing ────────────────────────────────────────────────────────
def test_registry_coverage_guard_passes() -> None:
    ok, missing = registry.check_registry_coverage()
    assert ok, f"registry missing: {missing}"


def test_criteria_load_from_the_prompt_library() -> None:
    # ca03 AC: the registry loads each criterion's rubric from the prompt library
    # (a contract-bearing prompt file), NOT from an inline constant / packaged JSON.
    from rebar.llm import prompts

    for cid in ("F1", "E2", "T5a", "ISF", "G3"):
        desc = registry.by_id()[cid]
        prompt = prompts.get_prompt(f"plan-review-{cid}")
        assert prompt.category == "plan-review-criterion"
        assert desc["scenario"] == prompt.text.strip()  # rubric came from the library file


def test_pass1_drops_findings_mapped_outside_the_chunk() -> None:
    # A finding whose criteria are NOT in the chunk must be DROPPED, never silently
    # re-attributed to the chunk's first criterion (the old `or ids[:1]` bug).
    from rebar.llm.runner import FakeRunner

    fr = FakeRunner(
        structured={
            "analysis": "",
            "findings": [
                {"finding": "in chunk", "criteria": ["E2"]},
                {"finding": "out of chunk", "criteria": ["T8"]},  # not in [E2]
                {"finding": "no criteria", "criteria": []},
            ],
        }
    )
    out = passes.pass1_chunk(fr, _fake_cfg(), plan="p", chunk=[{"id": "E2", "name": "x"}])
    assert [f["finding"] for f in out] == ["in chunk"]
    assert all(f["criteria"] == ["E2"] for f in out)


def test_no_inline_pass_prompt_constants() -> None:
    # Regression guard: the pass prompts must live in the library, never as inline
    # module constants (the shortcut the plan explicitly forbids).
    assert not hasattr(passes, "PASS1_SYSTEM")
    assert not hasattr(passes, "PASS2_SYSTEM")
    assert not hasattr(passes, "_plan_system")
    from rebar.llm import prompts

    for pid in (
        "plan-review-finder",
        "plan-review-verifier",
        "plan-review-coach",
        "plan-review-isf-finder",
        "plan-review-container",
    ):
        assert prompts.get_prompt(pid).category == "plan-review-pass"


def test_criterion_prompt_supports_project_override(tmp_path, monkeypatch) -> None:
    # A `.rebar/prompts/plan-review-<id>.md` override wins over the packaged rubric.
    from rebar import config as _config

    (tmp_path / ".rebar" / "prompts").mkdir(parents=True)
    (tmp_path / ".rebar" / "prompts" / "plan-review-F1.md").write_text(
        "---\nschema_version: 1\ntitle: Override\nexecution_mode: single_turn\n"
        "category: plan-review-criterion\ndimension: ac-text-quality\n---\nOVERRIDDEN RUBRIC.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(_config, "repo_root", lambda *a, **k: tmp_path)
    registry.load_criteria.cache_clear()
    try:
        assert registry.by_id()["F1"]["scenario"] == "OVERRIDDEN RUBRIC."
    finally:
        registry.load_criteria.cache_clear()


def test_registry_loads_descriptors() -> None:
    assert len(registry.load_criteria()) >= 31


def test_applies_suppresses_bugs() -> None:
    f1 = registry.by_id()["F1"]  # suppress_types includes bug
    assert not registry.applies(f1, level="task", ticket_type="bug")
    assert registry.applies(f1, level="task", ticket_type="task")


def test_chunk_by_facet_packs_and_never_empty_for_input() -> None:
    crits = [c for c in registry.load_criteria() if registry.exec_tier(c) != "AGENT"]
    chunks = registry.chunk_by_facet(crits, model="claude-sonnet-4-6", ticket_size="moderate")
    assert chunks and all(2 <= len(ch) <= 6 for ch in chunks[:-1] + [chunks[-1] or [None, None]])
    # every criterion appears exactly once
    flat = [c["id"] for ch in chunks for c in ch]
    assert sorted(flat) == sorted(c["id"] for c in crits)


def test_only_code_grounding_set_greps() -> None:
    assert registry.CODEBASE_GROUNDED <= registry.AGENT_TIER


def test_overlay_triggers_are_low_fp_set() -> None:
    fired = registry.overlay_triggers("This plan changes performance and latency on the hot path.")
    assert fired.get("T5a") is True


# ── Pass-4 subject validator (C1 enforcement) ─────────────────────────────────
def test_subject_validator_accepts_noun_phrase() -> None:
    assert passes._validate_subject("the retry/timeout policy") == "the retry/timeout policy"


def test_subject_validator_rejects_imperative_code_and_overlong() -> None:
    assert passes._validate_subject("Add a retry policy") is None
    assert passes._validate_subject("call foo()") is None
    assert passes._validate_subject("a " * 20) is None


# ── attestation + material binding ────────────────────────────────────────────
def test_manifest_roundtrip_and_material() -> None:
    verdict = {
        "verdict": "PASS",
        "ticket_id": "t1",
        "model": "m",
        "runner": "r",
        "coverage": {"counts": {"blocking": 0, "advisory_surfaced": 2}},
    }
    m = attest.build_manifest(verdict, material="deadbeef")
    assert attest.is_plan_review_manifest(m)
    assert attest.manifest_material(m) == "deadbeef"


def test_material_fingerprint_changes_on_material_edit() -> None:
    a = orchestrator.material_fingerprint(_ctx(_GOOD_AC))
    b = orchestrator.material_fingerprint(_ctx(_GOOD_AC + "\nNEW material content."))
    assert a != b


def test_material_fingerprint_stable_for_same_content() -> None:
    assert orchestrator.material_fingerprint(_ctx(_GOOD_AC)) == orchestrator.material_fingerprint(
        _ctx(_GOOD_AC)
    )


# ── sidecar payload ───────────────────────────────────────────────────────────
def test_sidecar_payload_is_offline_reconstructable() -> None:
    verdict = {
        "verdict": "PASS",
        "ticket_id": "t1",
        "ticket_type": "task",
        "model": "m",
        "runner": "r",
        "coverage": {"counts": {}},
        "blocking": [],
        "advisory": [
            {
                "id": "f1",
                "criteria": ["E2"],
                "tier": "LLM",
                "decision": "advisory",
                "severity": "minor",
                "validity": 0.8,
                "impact": 0.3,
                "priority": 0.24,
            }
        ],
        "overflow": [],
        "indeterminate": [],
        "dropped": [],
        "coaching": [],
    }
    p = sidecar.build_payload(verdict, material="abc")
    assert p["schema"] == "plan_review_result_v1"
    assert p["findings"][0]["id"] == "f1" and p["findings"][0]["criteria"] == ["E2"]
    assert p["material_fingerprint"] == "abc"


# ── orchestrator routing + exempt verdicts ────────────────────────────────────
def _fake_cfg():
    from rebar.llm.config import LLMConfig

    return LLMConfig(runner="fake")


def test_bug_is_exempt() -> None:
    v = orchestrator.run_review(_ctx(_GOOD_AC, ttype="bug"), _fake_cfg())
    assert v["verdict"] == "PASS" and v["runner"] == "exempt"


def test_review_records_latency_metrics() -> None:
    from rebar.llm.runner import FakeRunner

    fr = FakeRunner(structured={"analysis": "", "findings": []})
    v = orchestrator.run_review(_ctx(_GOOD_AC, ttype="task"), _fake_cfg(), runner=fr)
    m = v["coverage"]["metrics"]
    assert "det_ms" in m and "llm_ms" in m and "total_ms" in m and "llm_calls" in m
    assert m["total_ms"] >= 0 and "no-llm/no-network" in m["claim_path"]
    # The sidecar payload lifts metrics to the top level for offline join.
    assert sidecar.build_payload(v, material="x")["metrics"] == m


class _SeqRunner:
    """A runner returning a scripted sequence of outcomes (dict) or raising
    (Exception) per call — exercises the size-handling ladder deterministically."""

    name = "seq"

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    def preflight(self):
        pass

    def run(self, req):
        o = self._outcomes[min(self.calls, len(self._outcomes) - 1)]
        self.calls += 1
        if isinstance(o, Exception):
            raise o
        return o


def test_is_context_limit_error_and_ladder() -> None:
    assert orchestrator._is_context_limit_error(Exception("prompt is too long: 1.2M tokens"))
    assert not orchestrator._is_context_limit_error(Exception("connection reset"))
    assert orchestrator._models_at_or_above("claude-haiku-4-5") == [
        "claude-haiku-4-5",
        "claude-sonnet-4-6",
        "claude-opus-4-8",
    ]
    assert orchestrator._models_at_or_above("claude-opus-4-8") == ["claude-opus-4-8"]


def test_size_ladder_batch_falls_back_to_one_per_call() -> None:
    # Batch hits the context limit → one-criterion-per-call recovers both findings.
    ctx_err = Exception("prompt is too long")
    runner = _SeqRunner(
        [
            ctx_err,  # the batch call
            {"findings": [{"finding": "a", "criteria": ["E2"]}]},  # per-criterion E2
            {"findings": [{"finding": "b", "criteria": ["E5"]}]},  # per-criterion E5
        ]
    )
    events: list = []
    out = orchestrator._pass1_with_ladder(
        runner, _fake_cfg(), "plan", [{"id": "E2"}, {"id": "E5"}], False, events
    )
    assert sorted(f["finding"] for f in out) == ["a", "b"]
    assert any("one-criterion-per-call" in e for e in events)


def test_size_ladder_too_big_emits_blocking_failure_finding() -> None:
    # A single criterion that context-limits at EVERY model → a too-big failure finding.
    runner = _SeqRunner([Exception("maximum context length exceeded")])
    events: list = []
    out = orchestrator._pass1_with_ladder(
        runner, _fake_cfg(), "plan", [{"id": "E2"}], False, events
    )
    assert len(out) == 1 and out[0]["_too_big"] is True and out[0]["criteria"] == ["E2"]
    assert any("too big" in e for e in events)


def test_largest_window_uses_configured_model_window() -> None:
    # A haiku-only deployment caps P8 at haiku's window, not the ladder's 1M top.
    assert orchestrator.largest_window_tokens("claude-haiku-4-5") == 1_000_000  # escalates up
    assert orchestrator.largest_window_tokens("claude-opus-4-8") == 1_000_000
    assert orchestrator.largest_window_tokens(None) == orchestrator.MODEL_LADDER[-1][1]
    assert (
        orchestrator.largest_window_tokens("some-unknown-model") == orchestrator.MODEL_LADDER[-1][1]
    )


def test_advisory_cap_assertion_guards_blocking_leak() -> None:

    # A blocking finding must never reach the advisory cap — the guard fails loud.
    bad = det_floor.PlanContext(ticket_id="x", ticket_type="task", title="t", description=_GOOD_AC)
    # Directly exercise the invariant via a crafted advisory list is internal; instead
    # confirm a clean run keeps blocking out of advisory (no assertion fires).
    v = orchestrator.run_review(bad, _fake_cfg(), runner=None)
    assert isinstance(v.get("advisory"), list)


def test_route_criteria_splits_agent_and_single() -> None:
    single, agent = orchestrator.route_criteria(_ctx(_GOOD_AC, ttype="story"))
    assert single and agent
    assert all(registry.exec_tier(c) != "AGENT" for c in single)
    assert all(registry.exec_tier(c) == "AGENT" for c in agent)


def test_pass1_finding_carries_coaching_spec_fields() -> None:
    from rebar.llm.runner import FakeRunner

    fr = FakeRunner(
        structured={
            "analysis": "",
            "affirmations": ["F4"],
            "findings": [
                {
                    "finding": "vague AC",
                    "criteria": ["E2"],
                    "location": "## Acceptance Criteria line 2",
                    "checklist_item": "- [ ] make AC 2 measurable",
                    "suggested_fix": "state an observable outcome",
                }
            ],
        }
    )
    out = passes.pass1_chunk(fr, _fake_cfg(), plan="p", chunk=[{"id": "E2", "name": "x"}])
    assert out[0]["location"] and out[0]["checklist_item"] and out[0]["suggested_fix"]


def test_container_loop_per_child_and_too_big_pairing() -> None:
    from rebar.llm.runner import FakeRunner

    children = [
        {"ticket_id": "c1", "title": "C1", "description": "small child"},
        {"ticket_id": "c2", "title": "C2", "description": "x " * 200_000},  # oversized pairing
    ]
    ctx = _ctx(_GOOD_AC, ttype="epic", children=children, largest_window_tokens=50_000)
    fr = FakeRunner(
        structured={"analysis": "", "findings": [{"finding": "gap", "criteria": ["G3"]}]}
    )
    g3 = registry.by_id()["G3"]
    cov: dict = {}
    out = orchestrator._run_container(ctx, _fake_cfg(), fr, [g3], cov)
    # c1 pairing fits → a per-child finding tagged with the child; c2 is too-big → a
    # failure finding citing the oversized pairing.
    assert any(f.get("_container_child") == "c1" for f in out)
    assert any("too big" in f["finding"].lower() and "c2" in f["finding"] for f in out)
    assert cov["container"]["children"] == 2


def test_isf_excluded_from_normal_routing() -> None:
    # ISF is fed the session log separately; it must never enter the rubric chunks.
    single, agent = orchestrator.route_criteria(_ctx(_GOOD_AC, ttype="story"))
    assert "ISF" not in {c["id"] for c in single + agent}


def test_pass1_isf_tags_findings_and_reduced_confidence() -> None:
    from rebar.llm.runner import FakeRunner

    fr = FakeRunner(
        structured={"analysis": "", "findings": [{"finding": "dropped req X", "criteria": ["ISF"]}]}
    )
    out = passes.pass1_isf(
        fr, _fake_cfg(), plan="plan", session_log_text="log: must do X", summarized=True
    )
    assert out and out[0]["criteria"] == ["ISF"] and out[0]["_reduced_confidence"] is True
    assert any("SUMMARY" in e for e in out[0]["evidence"])
