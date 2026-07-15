"""Unit tests for the plan-review gate's deterministic core (epic 5fd2).

These exercise the pure, I/O-free seams — the DET floor (P1-P9) on synthetic
contexts, the Pass-3 deterministic math, the criteria registry + routing, the
Pass-4 subject validator, and the attestation/sidecar helpers — with no git store,
no LLM, no network. The store/CLI/MCP integration is pinned in
tests/interfaces/test_plan_review_gate.py.
"""

from __future__ import annotations

import pytest

from rebar.llm.config import DEFAULT_MODEL, VERIFIER_DEFAULT_MODEL, LLMConfig
from rebar.llm.plan_review import (
    _verifier_cfg,
    attest,
    det_floor,
    orchestrator,
    passes,
    registry,
    sidecar,
    sizing,
)
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


# ── verifier model downgrade (WS2 — gawky-koi-grain) ─────────────────────────
def test_verifier_cfg_downgrades_to_sonnet_by_default() -> None:
    """With no explicit operator model (cfg.model == DEFAULT_MODEL), the Pass-2 verify/coach
    cfg resolves to the non-frontier verifier model (Sonnet) — the verifier_cfg downgrade,
    now in the workflow path instead of the retired bespoke pass2_verify."""
    cfg = LLMConfig(model=DEFAULT_MODEL)
    assert _verifier_cfg(cfg).model == VERIFIER_DEFAULT_MODEL == "claude-sonnet-4-6"


def test_verifier_cfg_honors_explicit_operator_override() -> None:
    """An operator who explicitly chose a non-default model keeps it — the downgrade yields
    to the override (parity with the old passes.verifier_cfg), which a static per-step
    `model:` could not do (resolve_model precedence is step > workflow > cfg)."""
    cfg = LLMConfig(model="claude-opus-4-8-custom")
    assert _verifier_cfg(cfg).model == "claude-opus-4-8-custom"
    # Other config fields are preserved (only model is tuned).
    cfg2 = LLMConfig(model=DEFAULT_MODEL, max_iterations=99)
    assert _verifier_cfg(cfg2).max_iterations == 99


# ── Pass-2 verify token-budget chunking (WS3 — tangly-shunt-scoop) ───────────
def _finding(i: int, *, big: bool = False) -> dict:
    return {"finding": ("X" * 20_000) if big else f"finding number {i}", "criteria": ["E1"]}


def test_verify_chunks_single_call_for_small_sets() -> None:
    """Common case: the whole verify request fits the window → ONE chunk covering all
    findings with their global indices (byte-identical to the prior single aggregate call)."""
    findings = [_finding(i) for i in range(5)]
    chunks, omitted = sizing.verify_request_chunks(findings, model="claude-sonnet-4-6")
    assert len(chunks) == 1
    assert [idx for idx, _ in chunks[0]] == [0, 1, 2, 3, 4]
    assert omitted == []


def test_verify_chunks_splits_over_budget_preserving_global_indices(monkeypatch) -> None:
    """A deterministic window-shrink seam forces a small budget so a small findings set
    EXCEEDS it → principled token-based splitting into multiple calls (NOT a magic count),
    preserving GLOBAL indices, and every emitted chunk's estimated request fits the budget
    (the chars/4 estimate + headroom keeps each call under the window)."""
    # Shrink the window so ~one finding fits per chunk (budget just above the system reserve).
    budget = sizing.VERIFY_SYSTEM_RESERVE_TOKENS + sizing.PER_FINDING_VERIFY_TOKENS + 50
    monkeypatch.setattr(sizing, "largest_window_tokens", lambda model: budget)
    findings = [_finding(i) for i in range(6)]
    chunks, omitted = sizing.verify_request_chunks(
        findings, model="claude-sonnet-4-6", headroom=1.0
    )
    assert len(chunks) > 1, "a >budget set must split into multiple verify calls"
    assert omitted == []
    # Global indices are contiguous + complete across the chunks (re-merge by index works).
    flat = [idx for chunk in chunks for idx, _ in chunk]
    assert flat == list(range(6))
    # Every chunk's estimated request is within budget (the safety margin is grounded).
    from rebar.llm.plan_review import passes as _passes
    from rebar.llm.plan_review.det_floor import est_tokens

    for chunk in chunks:
        req = (
            est_tokens(_passes.verify_instructions(chunk))
            + sizing.VERIFY_SYSTEM_RESERVE_TOKENS
            + len(chunk) * sizing.PER_FINDING_VERIFY_TOKENS
        )
        assert req <= budget, (req, budget)


def test_verify_chunks_omits_finding_too_big_to_verify(monkeypatch) -> None:
    """A single finding whose own request exceeds the budget at the largest reachable model
    is OMITTED from every chunk (its index returned in `omitted`) — left unverified so pass3
    routes it to INDETERMINATE, never silently dropped."""
    budget = sizing.VERIFY_SYSTEM_RESERVE_TOKENS + sizing.PER_FINDING_VERIFY_TOKENS + 50
    monkeypatch.setattr(sizing, "largest_window_tokens", lambda model: budget)
    findings = [_finding(0), _finding(1, big=True), _finding(2)]  # index 1 is oversized
    chunks, omitted = sizing.verify_request_chunks(
        findings, model="claude-sonnet-4-6", headroom=1.0
    )
    assert omitted == [1]
    flat = [idx for chunk in chunks for idx, _ in chunk]
    assert 1 not in flat and set(flat) == {0, 2}


def test_merge_chunked_outputs_concatenates_verifications() -> None:
    """RunnerAgentStep merges per-chunk verify outputs by concatenating list fields
    (verifications) in order; a single chunk passes through unchanged."""
    from rebar.llm.workflow.runs import _merge_chunked_outputs

    one = {"verifications": [{"index": 0}], "runner": "fake", "model": None}
    assert _merge_chunked_outputs([one]) is one  # single chunk: unchanged
    merged = _merge_chunked_outputs(
        [
            {"verifications": [{"index": 0}], "runner": "fake", "model": "m"},
            {"verifications": [{"index": 1}, {"index": 2}], "runner": "fake", "model": "m"},
        ]
    )
    assert [v["index"] for v in merged["verifications"]] == [0, 1, 2]
    assert merged["runner"] == "fake" and merged["model"] == "m"  # scalars stable


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


def _oracle_outcome(kind: str):
    """A fake grounding.refute_absence for each fail-open mode."""
    if kind == "crash":
        return lambda *a, **k: (_ for _ in ()).throw(RuntimeError("backend crashed"))
    if kind == "timeout":
        return lambda *a, **k: (_ for _ in ()).throw(TimeoutError("ctags timed out"))
    if kind == "no-server":
        return lambda *a, **k: (_ for _ in ()).throw(ConnectionError("deps.dev unreachable"))
    if kind == "unsupported-lang":
        # The oracle's three-valued contract: an unsupported lang → an `abstain` record.
        return lambda *a, **k: {"outcome": "abstain", "reason": "unsupported_language"}
    raise AssertionError(kind)


@pytest.mark.parametrize("mode", ["crash", "timeout", "no-server", "unsupported-lang"])
def test_p2_p3_fail_open_per_tool(monkeypatch, mode: str) -> None:
    # Per-tool fail-open (epic AC): a missing tool / unsupported-lang / no-server /
    # crash / timeout at the oracle layer ABSTAINS (skipped=pass) — never blocks, never
    # raises. Asserted for EACH named mode.
    import rebar.grounding as grounding

    ctx = _ctx(_GOOD_AC + "\nWe touch `src/foo/bar.py` and pip install leftpad.", repo_root="/x")
    monkeypatch.setattr(grounding, "refute_absence", _oracle_outcome(mode))
    p2 = det_floor.p2_resolution(ctx)
    p3 = det_floor.p3_package_existence(ctx)
    assert not p2.blocking and p2.status in ("pass", "abstain")
    assert not p3.blocking and p3.status in ("pass", "abstain")


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


def test_pass3_absence_claim_veto() -> None:
    """a8e5 Component 1 (plan-review re-export): the absence-claim veto drops a finding whose
    absence premise the verifier refuted (a provision WAS found in the plan)."""
    v = _verif(binary={"claims_absence": "yes", "absence_confirmed_in_context": "no"})
    d = passes.pass3_decide(v)
    assert d["decision"] == "dropped" and d["reason"] == "veto:absence-refuted"


# ── a8e5 Component 2: DET-tier hygiene backstop (subject-less DET findings drop) ──────────────
def test_det_hygiene_drops_subjectless_det_finding() -> None:
    """A DET finding that names NO subject — no ``location`` and no ``evidence`` spans — is
    unadjudicable and is dropped at the DET emission/aggregation point (both lanes). This
    NEVER touches LLM-tier findings (they don't flow through det_*_findings)."""
    from rebar.llm.plan_review.det_floor import (
        DetResult,
        det_advisory_findings,
        det_blocking_findings,
        det_finding_has_subject,
    )

    assert det_finding_has_subject({"finding": "x", "evidence": ["a concrete span"]}) is True
    assert det_finding_has_subject({"finding": "x", "location": "src/foo.py:10"}) is True
    assert det_finding_has_subject({"finding": "x", "evidence": []}) is False

    subjectless_adv = DetResult(
        "P4", "p4_oversize", "fail", blocking=False, finding={"finding": "off", "evidence": []}
    )
    assert det_advisory_findings([subjectless_adv]) == []
    subjectless_block = DetResult(
        "P1", "p1_readiness_shape", "fail", blocking=True, finding={"finding": "no", "evidence": []}
    )
    assert det_blocking_findings([subjectless_block]) == []


# ── a8e5 Component 3: operator-attested AC awareness ──────────────────────────────────────────
def test_operator_attested_ac_texts_parses_tagged_criteria() -> None:
    """The pure DET parser extracts ONLY criteria tagged with the exact `[operator-attested]`
    token (case-insensitive), returning their criterion text (tag stripped)."""
    from rebar.llm.plan_review.workflow_ops import operator_attested_ac_texts

    desc = (
        "## Acceptance Criteria\n"
        "- [ ] a normal codebase-verifiable criterion\n"
        "- [ ] [operator-attested] the fix is deployed to prod and the two-vote gate passes\n"
    )
    texts = operator_attested_ac_texts(desc)
    assert len(texts) == 1
    assert "deployed to prod" in texts[0].lower()


def test_enrich_operator_attested_clears_ac_unverifiable_upstream() -> None:
    """A finding flagging an operator-attested AC as in-session-unverifiable gets
    ``operator_attested=True`` injected and its ``ac_unverifiable`` axis CLEARED to "none"
    BEFORE impact_plan reads it — so the hard-override 0.85 floor no longer fires. The kernel
    impact_plan math is unchanged; the fact is injected upstream."""
    from rebar.llm import review_kernel
    from rebar.llm.plan_review.workflow_ops import enrich_operator_attested

    desc = (
        "## Acceptance Criteria\n"
        "- [ ] [operator-attested] the fix is deployed to prod and the two-vote gate passes\n"
    )
    findings = [
        {
            "finding": "the AC cannot be objectively verified as written",
            "location": "## Acceptance Criteria",
            "evidence": ["the fix is deployed to prod and the two-vote gate passes"],
        }
    ]
    verifs = {0: {"severity_attributes": {"ac_unverifiable": "missing_oracle"}, "binary": {}}}
    assert review_kernel.impact_plan(verifs[0]["severity_attributes"]) >= 0.85
    enrich_operator_attested(findings, verifs, desc)
    attrs = verifs[0]["severity_attributes"]
    assert attrs.get("operator_attested") is True
    assert attrs.get("ac_unverifiable") == "none"
    assert review_kernel.impact_plan(attrs) < 0.85


# ── registry + routing ────────────────────────────────────────────────────────
def test_registry_coverage_guard_passes() -> None:
    ok, missing = registry.check_registry_coverage()
    assert ok, f"registry missing: {missing}"


def test_criteria_load_from_the_prompt_library() -> None:
    # ca03 AC: the registry loads each criterion's rubric from the prompt library
    # (a contract-bearing prompt file), NOT from an inline constant / packaged JSON.
    from rebar.llm.prompting import prompts

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
    from rebar.llm.prompting import prompts

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
    registry._load_criteria_cached.cache_clear()
    try:
        assert registry.by_id()["F1"]["scenario"] == "OVERRIDDEN RUBRIC."
    finally:
        registry._load_criteria_cached.cache_clear()


def test_registry_loads_descriptors() -> None:
    assert len(registry.load_criteria()) >= 31


def test_applies_suppresses_bugs() -> None:
    f1 = registry.by_id()["F1"]  # suppress_types includes bug
    assert not registry.applies(f1, has_children=False, ticket_type="bug")
    assert registry.applies(f1, has_children=False, ticket_type="task")


def test_applies_is_keyed_on_container_leaf_not_type() -> None:
    # The security overlay T5c is no longer altitude-gated: it runs on a CONTAINER
    # (the Gerrit-epic regression) as well as a leaf, regardless of ticket type.
    t5c = registry.by_id()["T5c"]
    assert registry.applies(t5c, has_children=True)  # container epic — the fix
    assert registry.applies(t5c, has_children=False)  # leaf
    # Container-scoped child-coverage criteria (G3/G4) run ONLY on containers.
    g3 = registry.by_id()["G3"]
    assert registry.applies(g3, has_children=True)
    assert not registry.applies(g3, has_children=False)
    # Leaf-scoped implementation criteria (E4 code-grounding) run ONLY on leaves —
    # and a CHILDLESS ticket of ANY type (e.g. a leaf-shaped epic) is a leaf.
    e4 = registry.by_id()["E4"]
    assert registry.applies(e4, has_children=False, ticket_type="epic")
    assert not registry.applies(e4, has_children=True, ticket_type="epic")


def test_t10_carries_endpoint_access_contract_check() -> None:
    # The infra overlay must check that a stood-up network-reachable service declares
    # its human/admin auth contract (the bug a278 regression: the Gerrit epic stood up
    # an internet-facing service with no auth). It runs on containers AND leaves, and
    # only on infra intent (LLM-routed) so it is FP-safe on non-infra tickets.
    t10 = registry.by_id()["T10"]
    keys = {c["key"] for c in t10["checklist"]}
    assert "endpoint_access_contract" in keys
    bullet = next(c for c in t10["checklist"] if c["key"] == "endpoint_access_contract")
    text = bullet["check"].lower()
    # The distinguishing content: HUMAN/admin auth, and that machine creds do NOT satisfy it.
    assert "human" in text and "auth" in text
    assert "deploy key" in text or "token" in text  # service-to-service creds are called out
    assert registry.applies(t10, has_children=True)  # fires on a container epic (the fix)
    assert registry.applies(t10, has_children=False)  # and on a leaf


def test_t5c_leads_with_trust_boundary_scope_gate() -> None:
    # Ticket 2e89: T5c must lead with an explicit trust-boundary scope gate that generalises
    # across its dimensions, preserves the in-process/loopback positive-pass carve-out, and
    # encodes the zero-trust "private ≠ exempt" caveat.
    t5c = registry.by_id()["T5c"]
    # The checklist LEADS with the trust-boundary gate, and the per-dimension checks are kept.
    keys = [c["key"] for c in t5c["checklist"]]
    assert keys[0] == "trust_boundary"
    dims = {"access_classification", "data_protection", "least_privilege", "secret_lifecycle"}
    assert dims <= set(keys)
    gate = next(c for c in t5c["checklist"] if c["key"] == "trust_boundary")["check"].lower()
    assert "lower-trust" in gate or "lower trust" in gate
    assert "not-applicable" in gate or "not applicable" in gate  # the positive-pass carve-out
    assert "zero-trust" in gate or "not exempt" in gate  # the zero-trust caveat
    # The rubric body (scenario) carries the same framing: the gate, the carve-out, the
    # mixed-scope + ambiguous-reachability rules, and the T10 no-blur scope note.
    body = t5c.get("scenario", "").lower()
    assert "trust-boundary" in body and "reachable by a lower-trust actor" in body
    assert "loopback" in body and "not-applicable" in body  # carve-out preserved (FP-free)
    assert "mixed-scope" in body  # sub-checks scoped to boundary-crossing components only
    assert "zero-trust" in body  # single-tenant/private = lower severity, not exempt
    assert "t10" in body and "no blurring" in body  # explicit no-blur scope note
    # Still altitude-agnostic (fires on container AND leaf, per a278).
    assert registry.applies(t5c, has_children=True)
    assert registry.applies(t5c, has_children=False)


def test_t10_not_reframed_by_trust_boundary_generalisation() -> None:
    # Ticket 2e89 AC: the T5c trust-boundary generalisation must NOT blur into T10 — T10 keeps
    # its infra facet, its LLM-routed infra-intent trigger (FP-safe on non-infra tickets), and
    # its endpoint_access_contract check, and is NOT re-scoped to the general framing.
    t10 = registry.by_id()["T10"]
    assert t10["facet"] == "overlay-infra"  # unchanged — not folded into overlay-security
    assert t10.get("overlay_routing") == "llm"  # still LLM-routed on infra intent only
    assert "infrastructure" in t10["trigger"].lower() or "iac" in t10["trigger"].lower()
    keys = {c["key"] for c in t10["checklist"]}
    assert "endpoint_access_contract" in keys  # its own contract check is intact
    # T10 did NOT inherit T5c's trust_boundary scope-gate key (no blurring).
    assert "trust_boundary" not in keys


def test_is_mechanical_leaf_keys_on_leaf_not_type() -> None:
    plan = "Refactor the module; rename the helper."
    assert registry.is_mechanical_leaf(plan, has_children=False)
    assert not registry.is_mechanical_leaf(plan, has_children=True)  # a container is never a leaf


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
def test_pass4_coach_maps_findings_and_renders_deterministically() -> None:
    from rebar.llm.runner import FakeRunner

    moves = orchestrator.MOVE_REGISTRY
    fr = FakeRunner(
        structured={
            "notes": [
                {"move_id": "1", "subject": "the retry/timeout policy", "finding_refs": ["f1"]},
                # invalid subject (imperative) → dropped (C1 fallback)
                {"move_id": "5", "subject": "Add a cache", "finding_refs": ["f2"]},
                # unknown move id → dropped
                {"move_id": "99", "subject": "the thing", "finding_refs": ["f3"]},
            ]
        }
    )
    notes = passes.pass4_coach(
        fr, _fake_cfg(), plan="p", surviving=[{"id": "f1", "finding": "x"}], move_registry=moves
    )
    assert len(notes) == 1  # only the valid move+subject survives
    n = notes[0]
    assert n["move_id"] == "1" and n["finding_refs"] == ["f1"]
    # Prose rendered DETERMINISTICALLY from the locked template (the LLM didn't author it).
    assert n["coaching"] == moves["1"]["template"].format(subject="the retry/timeout policy")


def test_move_registry_foundation_enhancement_and_no_defer() -> None:
    # WS8 (epic cite-stone-sea): the foundation/enhancement move (10) is the follow-on route that
    # REPLACES a DEFERRED_MEASUREMENT move — which must NOT exist (a blocking AC must be
    # in-session-closable). It is scoped (applies_when) to sizing/complexity/risk criteria, and
    # move 9 is sharpened to cover restating a deferred/unobservable target as an observable proxy.
    reg = orchestrator.MOVE_REGISTRY
    m10 = reg.get("10", {})
    assert "foundation" in m10.get("name", "").lower()
    assert "follow-on" in m10.get("template", "").lower(), "move 10 routes to a dependent follow-on"
    assert m10.get("applies_when"), "move 10 must be scoped via applies_when"
    assert set(m10["applies_when"]) <= {"G5", "A1", "T2"}
    # NO defer / DEFERRED_MEASUREMENT move (dropped as counter-architectural).
    blob = " ".join(f"{m.get('name', '')} {m.get('template', '')}" for m in reg.values()).lower()
    assert "deferred_measurement" not in blob and "defer the measurement" not in blob
    # move 9 sharpened to the verifiable-proxy restatement.
    assert "proxy" in reg["9"]["template"].lower()


def test_move_registry_matches_docs_table() -> None:
    # WS8 AC: docs/plan-review-gate.md's move table mirrors MOVE_REGISTRY id -> template.
    import re
    from pathlib import Path

    from rebar.llm.plan_review import passes

    doc = (Path(passes.__file__).parents[4] / "docs" / "plan-review-gate.md").read_text()
    rows = dict(re.findall(r"^\|\s*(\d+)\s*\|[^|]+\|\s*\"(.+?)\"\s*\|$", doc, re.MULTILINE))
    reg = orchestrator.MOVE_REGISTRY
    assert rows, "no move-table rows parsed from the doc"
    for mid, move in reg.items():
        assert rows.get(mid) == move["template"], f"doc row {mid} != MOVE_REGISTRY template"
    assert set(rows) == set(reg), "doc move table and MOVE_REGISTRY have different id sets"


def test_load_move_registry_merges_project_extensions(tmp_path) -> None:
    (tmp_path / ".rebar").mkdir()
    (tmp_path / ".rebar" / "plan_review_moves.json").write_text(
        '{"99": {"name": "custom move", "template": "Do custom thing for {subject}."},'
        ' "bad": {"name": "no placeholder", "template": "no subject slot"}}',
        encoding="utf-8",
    )
    reg = orchestrator.load_move_registry(repo_root=str(tmp_path))
    assert "1" in reg  # built-ins retained
    assert reg["99"]["name"] == "custom move"  # project move added
    assert "bad" not in reg  # rejected: template lacks {subject}


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


def test_manifest_deps_roundtrip_and_backcompat() -> None:
    verdict = {
        "verdict": "PASS",
        "ticket_id": "t1",
        "model": "m",
        "runner": "r",
        "coverage": {"counts": {}},
    }
    m = attest.build_manifest(verdict, material="x", deps={"src/b.py": "h2", "src/a.py": "h1"})
    # per-path map round-trips (sorted), and the manifest stays a valid plan-review one
    assert attest.manifest_deps(m) == {"src/a.py": "h1", "src/b.py": "h2"}
    assert attest.is_plan_review_manifest(m) and attest.manifest_material(m) == "x"
    # no deps → empty map: an attestation predating ADR 0002 parses cleanly
    assert attest.manifest_deps(attest.build_manifest(verdict, material="x")) == {}


# ── P9 file-impact coverage (ADR 0002) ─────────────────────────────────────────
def test_p9_warns_on_empty_file_impact_leaf() -> None:
    r = det_floor.p9_file_impact_coverage(_ctx(_GOOD_AC, ttype="task"))
    assert r.status == "fail" and not r.blocking and r.finding
    assert r.coverage["applicable"] is True


def test_p9_passes_when_file_impact_present() -> None:
    r = det_floor.p9_file_impact_coverage(
        _ctx(_GOOD_AC, ttype="task", state={"file_impact": [{"path": "src/x.py", "reason": "y"}]})
    )
    assert r.status == "pass"


def test_p9_not_applicable_for_container() -> None:
    r = det_floor.p9_file_impact_coverage(
        _ctx(_GOOD_AC, ttype="story", children=[{"ticket_id": "c1"}])
    )
    assert r.status == "pass" and r.coverage["applicable"] is False


# NOTE: the bespoke `orchestrator.run_review` path was retired (story B-RETIRE). Its
# INDETERMINATE-on-outage / per-criterion fail-open / bug-exempt / budget-shed behaviours are
# now produced + asserted on the workflow gate path:
#   - tests/unit/test_gate_engine_cutover.py (outage degradation, coach-failure recovery);
#   - tests/interfaces/lifecycle/test_plan_review_gate.py (deps/key outage, fail-open,
#     bug-exempt, cap-hit INDETERMINATE — all via review_plan on the workflow engine);
#   - tests/unit/workflow/test_plan_review_workflow.py (exempt short-circuit, decide partition).
# The per-pass LATENCY/COST metrics (db7b AC5: coverage["metrics"] det_ms/llm_ms/total_ms/
# llm_calls) were bespoke-ONLY (computed inside run_review) and were RETIRED with it — the
# workflow path does not yet emit them, so `coverage["metrics"]` is absent there. Reinstating
# passive latency/cost telemetry on the workflow gate is a documented follow-up (tracked
# separately), NOT covered above.


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
    assert p["schema"] == "plan_review_result_v2"
    assert p["findings"][0]["id"] == "f1" and p["findings"][0]["criteria"] == ["E2"]
    assert p["material_fingerprint"] == "abc"


# ── orchestrator routing + exempt verdicts ────────────────────────────────────
def _fake_cfg():

    return LLMConfig(runner="fake")


def test_checkpoint_save_resume_and_material_invalidation(tmp_path) -> None:
    ctx = _ctx(_GOOD_AC, repo_root=str(tmp_path))
    chunk = [{"id": "E2"}]
    assert sizing.load_checkpoint(ctx, "matFP", chunk, "m", False) is None  # cold miss
    sizing.save_checkpoint(ctx, "matFP", chunk, "m", False, [{"finding": "x", "criteria": ["E2"]}])
    got = sizing.load_checkpoint(ctx, "matFP", chunk, "m", False)  # resume
    assert got and got[0]["finding"] == "x"
    # A material edit (different fingerprint) ⇒ cache miss (stale checkpoint ignored).
    assert sizing.load_checkpoint(ctx, "OTHER_FP", chunk, "m", False) is None


def test_centrality_from_ticket_graph() -> None:
    state = {
        "deps": [
            {"relation": "blocks", "target_id": "a"},
            {"relation": "depends_on", "target_id": "b"},
            {"relation": "relates_to", "target_id": "c"},  # not a blast-radius edge
        ]
    }
    children = [{"ticket_id": "k1"}, {"ticket_id": "k2"}]
    # 2 blast edges + 2 children = 4/10 = 0.4
    assert orchestrator._centrality(state, children) == 0.4
    assert orchestrator._centrality({}, []) == 0.0


def test_budget_cap_scales_with_centrality(monkeypatch) -> None:
    monkeypatch.setenv("REBAR_PLAN_REVIEW_BUDGET", "1.0")
    low = sizing.plan_budget_cap(_ctx(_GOOD_AC))  # centrality 0 → 1.0
    high = sizing.plan_budget_cap(_ctx(_GOOD_AC, centrality=1.0))  # → 2.0
    assert low == 1.0 and high == 2.0


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
    assert orchestrator.largest_window_tokens(None) == sizing.MODEL_LADDER[-1][1]
    assert orchestrator.largest_window_tokens("some-unknown-model") == sizing.MODEL_LADDER[-1][1]


def test_advisory_cap_assertion_guards_blocking_leak() -> None:
    # A blocking finding must never reach the advisory cap — partition_findings asserts loud.
    # Feed a DET block + a DET advisory through the shared partition core and confirm the
    # block lands in `blocking` (never `surfaced`/`overflow`) with no AssertionError.
    parts = orchestrator.partition_findings(
        [{"finding": "no AC", "criteria": ["P1"]}],
        [{"finding": "minor", "criteria": ["E2"]}],
        [],
        advisory_cap=10,
    )
    assert len(parts["blocking"]) == 1 and parts["blocking"][0]["decision"] == "block"
    assert all(f.get("decision") != "block" for f in parts["surfaced"] + parts["overflow"])


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


def test_container_system_prompt_is_byte_stable_across_children() -> None:
    # Story 0250: the cached prefix is the container SYSTEM PROMPT (the whole parent
    # plan). For caching to read (not re-write) across the per-child fan-out, that
    # system prompt MUST be byte-identical for every child of the same parent — only
    # the per-child `instructions` (the user message) vary. Capture the RunRequest the
    # finder builds for two different children and assert the system prompts match.
    class _CapturingRunner:
        name = "capture"

        def __init__(self):
            self.system_prompts: list[str] = []

        def preflight(self):
            pass

        def run(self, req):
            self.system_prompts.append(req.system_prompt)
            return {"findings": []}

    cap = _CapturingRunner()
    g3 = registry.by_id()["G3"]
    parent_plan = _GOOD_AC + "\n## Context\nA parent plan with stable bytes.\n"
    for child in (
        {"ticket_id": "c1", "title": "C1", "description": "first child body"},
        {"ticket_id": "c2", "title": "C2", "description": "an entirely different body"},
    ):
        passes.pass1_container(
            cap,
            _fake_cfg(),
            parent_plan=parent_plan,
            children=[child],
            criteria=[g3],
            sibling_roster="- c1\n- c2",
        )
    assert len(cap.system_prompts) == 2
    assert cap.system_prompts[0] == cap.system_prompts[1]  # byte-stable ⇒ cacheable prefix


class _PairingRunner:
    """A thread-safe container runner that counts calls and can fail the FIRST call
    (story ba7e warm-then-fan-out tests). Returns one canned G3 finding per call so the
    aggregate finding count proves every in-budget pairing ran exactly once."""

    name = "pairing"

    def __init__(self, fail_first: str | None = None):
        import threading

        self.fail_first = fail_first  # None | "systemic" | "nonsystemic"
        self.calls = 0
        self._lock = threading.Lock()

    def preflight(self):
        pass

    def run(self, req):
        from rebar.llm.errors import LLMUnavailableError

        with self._lock:
            i = self.calls
            self.calls += 1
        if i == 0 and self.fail_first == "systemic":
            raise LLMUnavailableError("provider down")
        if i == 0 and self.fail_first == "nonsystemic":
            raise RuntimeError("transient non-systemic failure")
        return {"findings": [{"finding": f"f{i}", "criteria": ["G3"]}]}


def _big_epic_ctx(n_children: int, *, big_plan: bool):
    # Children sized + the window tightened so parent + ONE child fits a bin but parent +
    # TWO do not -> exactly one bin PER child (the fan-out granularity the warm-then-fan-out
    # tests exercise, after S5 bin-packing). Each child ~20000 tokens (larger than even the
    # big parent), with a window whose P8 budget is ~38000, so two children never co-pack
    # regardless of parent size. big_plan keeps the parent over the 4096-token cache floor.
    desc = ("padding word " * 5000) if big_plan else _GOOD_AC
    children = [
        {"ticket_id": f"c{i}", "title": f"C{i}", "description": "x " * 40000}
        for i in range(n_children)
    ]
    return _ctx(desc, ttype="epic", children=children, largest_window_tokens=77_778)


def test_container_warm_then_fan_out_runs_each_pairing_once() -> None:
    # A cacheable parent (>4096 tokens) + 3 children ⇒ warm ONE pairing, then fan out the
    # remaining 2. Every pairing runs exactly once (no dup/drop); coverage records warmed.
    ctx = _big_epic_ctx(3, big_plan=True)
    g3 = registry.by_id()["G3"]
    runner = _PairingRunner()
    cov: dict = {}
    out = orchestrator._run_container(ctx, _fake_cfg(), runner, [g3], cov)
    assert runner.calls == 3  # 1 warm + 2 fanned-out, each pairing once
    assert len([f for f in out if f.get("_container_child")]) == 3
    assert cov["container"]["warmed"] is True
    assert cov["container"]["parallel"] is True
    assert cov["container"]["pairings_evaluated"] == 3


def test_container_skips_warm_below_cache_floor() -> None:
    # A sub-floor parent plan never caches ⇒ warming would just serialize a call for no
    # read benefit, so fan out directly (warmed=False) — still every pairing once.
    ctx = _big_epic_ctx(3, big_plan=False)
    g3 = registry.by_id()["G3"]
    runner = _PairingRunner()
    cov: dict = {}
    out = orchestrator._run_container(ctx, _fake_cfg(), runner, [g3], cov)
    assert runner.calls == 3
    assert cov["container"]["warmed"] is False
    assert len([f for f in out if f.get("_container_child")]) == 3


def test_container_warm_systemic_failure_aborts() -> None:
    # A SYSTEMIC failure (LLMUnavailableError) on the warming call aborts the whole
    # fan-out — never fan out N-1 doomed calls.
    from rebar.llm.errors import LLMUnavailableError

    ctx = _big_epic_ctx(4, big_plan=True)
    g3 = registry.by_id()["G3"]
    runner = _PairingRunner(fail_first="systemic")
    with pytest.raises(LLMUnavailableError):
        orchestrator._run_container(ctx, _fake_cfg(), runner, [g3], {})
    assert runner.calls == 1  # aborted at the warm call; no fan-out


def test_container_warm_nonsystemic_failure_degrades_to_direct_fan_out() -> None:
    # A NON-systemic warm failure degrades to a direct fan-out of ALL pairings — the
    # failed pairing re-runs in the pool (not silently dropped), never hangs.
    ctx = _big_epic_ctx(3, big_plan=True)
    g3 = registry.by_id()["G3"]
    runner = _PairingRunner(fail_first="nonsystemic")
    cov: dict = {}
    out = orchestrator._run_container(ctx, _fake_cfg(), runner, [g3], cov)
    assert runner.calls == 4  # 1 failed warm + 3 in the pool (the failed pairing re-runs)
    assert cov["container"]["warmed"] is False
    assert len([f for f in out if f.get("_container_child")]) == 3  # no pairing dropped


def test_container_merges_g3_g4_into_one_call_per_child() -> None:
    # Story 98c6: G3+G4 are evaluated in ONE merged call per child (2N->N). With 3
    # children and 2 container criteria, the runner is called 3 times (not 6).
    ctx = _big_epic_ctx(3, big_plan=True)
    container = [registry.by_id()["G3"], registry.by_id()["G4"]]
    runner = _PairingRunner()
    cov: dict = {}
    orchestrator._run_container(ctx, _fake_cfg(), runner, container, cov)
    assert runner.calls == 3  # one merged call per child, NOT 2 per child (would be 6)
    assert cov["container"]["pairings_evaluated"] == 3


def test_container_attribution_is_self_reported_and_validated() -> None:
    # The merged call's attribution is MODEL-self-reported, validated against {G3,G4}:
    # in-set tags kept, OUT-of-set tags dropped, a finding mapping to no in-set criterion
    # dropped (not mis-tagged); _container_child provenance preserved.
    from rebar.llm.runner import FakeRunner

    fr = FakeRunner(
        structured={
            "analysis": "",
            "findings": [
                {"finding": "coverage gap", "criteria": ["G3"]},
                {"finding": "consistency + coverage", "criteria": ["G3", "G4"]},
                {"finding": "bogus tag", "criteria": ["G7"]},  # out of {G3,G4} -> dropped
                {"finding": "no tag", "criteria": []},  # no in-set criterion -> dropped
            ],
        }
    )
    g3, g4 = registry.by_id()["G3"], registry.by_id()["G4"]
    out = passes.pass1_container(
        fr,
        _fake_cfg(),
        parent_plan="PARENT",
        children=[{"ticket_id": "c1", "title": "C1", "description": "body"}],
        criteria=[g3, g4],
        sibling_roster="- c1",
    )
    assert [f["finding"] for f in out] == ["coverage gap", "consistency + coverage"]
    assert out[0]["criteria"] == ["G3"]
    assert out[1]["criteria"] == ["G3", "G4"]  # multi-criterion attribution preserved
    assert all(f["_container_child"] == "c1" for f in out)  # single-child bin -> sole child


def test_container_bin_packs_small_children_into_fewer_calls() -> None:
    # Story 1762: small children + a huge window pack into ONE merged bin -> ONE call (< N).
    ctx = _ctx(
        "padding word " * 5000,
        ttype="epic",
        children=[
            {"ticket_id": f"c{i}", "title": f"C{i}", "description": "tiny"} for i in range(4)
        ],
        largest_window_tokens=1_000_000,
    )
    container = [registry.by_id()["G3"], registry.by_id()["G4"]]
    runner = _PairingRunner()
    cov: dict = {}
    orchestrator._run_container(ctx, _fake_cfg(), runner, container, cov)
    assert cov["container"]["bins"] == 1  # 4 small children packed into a single bin
    assert runner.calls == 1  # ONE call for the whole bin (< 4)
    assert cov["container"]["pairings_evaluated"] == 1


def test_container_attributes_findings_per_child_in_a_packed_bin() -> None:
    # In a multi-child bin the model self-reports the child via `location` ('child <id>');
    # it is validated against the bin's children and preserved as _container_child.
    from rebar.llm.runner import FakeRunner

    fr = FakeRunner(
        structured={
            "analysis": "",
            "findings": [
                {"finding": "gap in a", "criteria": ["G3"], "location": "child a"},
                {"finding": "overlap in b", "criteria": ["G4"], "location": "child b"},
                {"finding": "bin-level", "criteria": ["G3"], "location": "somewhere"},
            ],
        }
    )
    g3, g4 = registry.by_id()["G3"], registry.by_id()["G4"]
    out = passes.pass1_container(
        fr,
        _fake_cfg(),
        parent_plan="PARENT",
        children=[
            {"ticket_id": "a", "title": "A", "description": "x"},
            {"ticket_id": "b", "title": "B", "description": "y"},
        ],
        criteria=[g3, g4],
        sibling_roster="- a\n- b",
    )
    by_finding = {f["finding"]: f for f in out}
    assert by_finding["gap in a"]["_container_child"] == "a"
    assert by_finding["overlap in b"]["_container_child"] == "b"
    # An unattributed multi-child finding stays bin-level (None), not mis-assigned.
    assert by_finding["bin-level"]["_container_child"] is None


def test_container_attribution_is_prefix_collision_safe() -> None:
    # A child id that is a PREFIX of another in the same bin ('c1' vs 'c12') must not be
    # mis-attributed: 'child c12' attributes to c12 (whole-token match), not c1.
    from rebar.llm.runner import FakeRunner

    fr = FakeRunner(
        structured={
            "analysis": "",
            "findings": [
                {"finding": "about c12", "criteria": ["G3"], "location": "child c12"},
                {"finding": "about c1", "criteria": ["G4"], "location": "child c1"},
            ],
        }
    )
    g3, g4 = registry.by_id()["G3"], registry.by_id()["G4"]
    out = passes.pass1_container(
        fr,
        _fake_cfg(),
        parent_plan="PARENT",
        children=[
            {"ticket_id": "c1", "title": "C1", "description": "x"},
            {"ticket_id": "c12", "title": "C12", "description": "y"},
        ],
        criteria=[g3, g4],
        sibling_roster="- c1\n- c12",
    )
    by_finding = {f["finding"]: f for f in out}
    assert by_finding["about c12"]["_container_child"] == "c12"  # NOT mis-matched to c1
    assert by_finding["about c1"]["_container_child"] == "c1"


def test_container_oversized_child_keeps_too_big_finding() -> None:
    # A child whose parent+child ALONE exceeds budget stays the single-child fallback ->
    # the existing too-big failure finding; the small child still packs + runs.
    ctx = _ctx(
        _GOOD_AC,
        ttype="epic",
        children=[
            {"ticket_id": "sm", "title": "S", "description": "tiny"},
            {"ticket_id": "big", "title": "B", "description": "x " * 200_000},  # ~100k tokens
        ],
        largest_window_tokens=50_000,
    )
    container = [registry.by_id()["G3"], registry.by_id()["G4"]]
    cov: dict = {}
    out = orchestrator._run_container(ctx, _fake_cfg(), _PairingRunner(), container, cov)
    assert any("too big" in f["finding"].lower() and "big" in f["finding"] for f in out)
    assert cov["container"]["bins"] == 1  # only the small child's bin runs


def test_container_floor_reflects_packed_bins_and_zero_without_container() -> None:
    # Story 1762: the budget container floor = packed BIN count * COST_AGENT (< N), and 0
    # when there is no container criterion (S4 cost-model assertion).
    children = [{"ticket_id": f"c{i}", "title": f"C{i}", "description": "tiny"} for i in range(4)]
    ctx = _ctx(_GOOD_AC, ttype="epic", children=children, largest_window_tokens=1_000_000)
    chunks = [[{"id": "E2"}]]
    cov: dict = {}
    sizing.shed_to_budget(ctx, chunks, [], [{"id": "G3"}, {"id": "G4"}], cov)
    assert cov["budget"]["container_floor_usd"] == round(1 * sizing.COST_AGENT_USD, 4)  # 1 bin
    cov2: dict = {}
    sizing.shed_to_budget(ctx, chunks, [], [], cov2)  # no container criteria
    assert cov2["budget"]["container_floor_usd"] == 0.0


def test_budget_cap_never_sheds_container_criteria() -> None:
    # With a tiny cap, AGENT/overlay criteria shed but G3/G4 are NEVER shed (shedding
    # them would drop child-coverage/consistency — the fidelity regression the epic
    # forbids). The cap bounds only the sheddable single-turn + agent spend.
    import os

    os.environ["REBAR_PLAN_REVIEW_BUDGET"] = "0.0"
    try:
        ctx = _ctx(_GOOD_AC, ttype="epic", children=[{"ticket_id": "c1"}, {"ticket_id": "c2"}])
        chunks = [[{"id": "E2"}]]
        agent = [{"id": "T8"}, {"id": "G6"}]  # an overlay + a core agent criterion
        container = [{"id": "G3"}, {"id": "G4"}]
        cov: dict = {}
        kept_agent, kept_container, shed = sizing.shed_to_budget(ctx, chunks, agent, container, cov)
        assert {c["id"] for c in kept_container} == {"G3", "G4"}  # container survives
        assert {c["id"] for c in shed}.isdisjoint({"G3", "G4"})  # nothing container shed
        assert cov["budget"]["container_never_shed"] is True
        assert kept_agent == [] and {c["id"] for c in shed} == {"T8", "G6"}  # agent fully shed
    finally:
        del os.environ["REBAR_PLAN_REVIEW_BUDGET"]


def test_isf_excluded_from_normal_routing() -> None:
    # ISF is fed the session log separately; it must never enter the rubric chunks.
    single, agent = orchestrator.route_criteria(_ctx(_GOOD_AC, ttype="story"))
    assert "ISF" not in {c["id"] for c in single + agent}


def test_ticket_graph_blob_includes_parent_children_links() -> None:
    ctx = _ctx(
        _GOOD_AC,
        ttype="story",
        state={
            "parent_id": "ep01",
            "deps": [{"relation": "relates_to", "target_id": "log01"}],
        },
        children=[{"ticket_id": "c1", "title": "child one"}],
    )
    blob = orchestrator._ticket_graph_blob(ctx)
    assert "parent: ep01" in blob and "c1: child one" in blob and "relates_to -> log01" in blob


def test_pass1_isf_is_fed_the_ticket_graph() -> None:
    from rebar.llm.runner import FakeRunner

    fr = FakeRunner(structured={"analysis": "", "findings": []})
    # No assertion on output beyond no-crash; the graph is rendered into instructions
    # (covered by _ticket_graph_blob above). Confirms the param is accepted + wired.
    out = passes.pass1_isf(
        fr, _fake_cfg(), plan="p", session_log_text="log", ticket_graph="parent: x"
    )
    assert out == []


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
