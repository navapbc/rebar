"""Unit tests for the plan-review :class:`ProductionBatchRunner` (epic B, story B1).

These are OFFLINE: a fake ``rebar.llm.Runner`` drives the finder (no model / network),
and ``rebar.show_ticket`` / ``rebar.list_tickets`` are monkeypatched so
``assemble_context`` reconstructs a synthetic ticket with no git store. The key AC is
BEHAVIOURAL EQUIVALENCE — the runner's findings equal those of the bespoke
``run_pass1`` path on the same context + criteria + fake runner (no duplicated
algorithm).
"""

from __future__ import annotations

import dataclasses
import re

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import registry
from rebar.llm.plan_review.orchestrator import assemble_context, route_criteria
from rebar.llm.plan_review.pass1 import run_pass1
from rebar.llm.plan_review.production_batch_runner import ProductionBatchRunner
from rebar.llm.runner import FakeRunner
from rebar.llm.workflow.runners import BatchRunRequest, BatchRunResult

_GOOD_AC = (
    "## Why\nthe system needs X.\n\n## What\nbuild X in `src/rebar/x.py`.\n\n"
    "## Scope\njust X.\n\n## Acceptance Criteria\n"
    "- [ ] X is observably true\n- [ ] another check\n"
)

_TARGET = "abcd-0000-0000-0001"


def _state(*, ttype: str = "story", description: str = _GOOD_AC) -> dict:
    return {
        "ticket_id": _TARGET,
        "ticket_type": ttype,
        "title": "Build X",
        "description": description,
        "deps": [],
    }


@pytest.fixture
def _stub_reads(monkeypatch):
    """Monkeypatch the rebar reads ``assemble_context`` uses so it reconstructs a
    synthetic, store-free ticket with no children."""

    state = _state()

    def _show(ticket_id, *, repo_root=None):  # noqa: ANN001
        return dict(state)

    def _list(*, parent=None, repo_root=None):  # noqa: ANN001
        return []

    monkeypatch.setattr("rebar._reads.show_ticket", _show)
    monkeypatch.setattr("rebar._reads.list_tickets", _list)
    return state


def _cfg() -> LLMConfig:
    # Match what ProductionBatchRunner builds: from_env (offline-safe) with the entry
    # model pinned to the ladder head used in the requests below.
    return dataclasses.replace(LLMConfig.from_env(repo_root=None), model="claude-opus-4-8")


def _make_req(criteria, *, target=_TARGET, usd_budget=None, repo_root=None) -> BatchRunRequest:
    return BatchRunRequest(
        finder="plan-review-finder",
        criteria=tuple(criteria),
        usd_budget=usd_budget,
        model_ladder=("claude-opus-4-8",),
        workflow={},
        target_ticket=target,
        repo_root=repo_root,
        run_id="run-1",
        step_id="finders",
    )


# ── equivalence: the runner ≡ the bespoke run_pass1 path (the key AC) ───────────
def test_runner_findings_equal_bespoke_run_pass1(_stub_reads):
    ctx = assemble_context(_TARGET, repo_root=None)
    single, agent = route_criteria(ctx)
    cfg = _cfg()

    # A deterministic fake that returns one finding per routed criterion. pass1_chunk
    # filters findings to the chunk's criteria, so each routed criterion surfaces its
    # own finding in BOTH paths.
    routed_ids = [c["id"] for c in single + agent]
    fake = FakeRunner(
        structured={
            "analysis": "",
            "findings": [{"finding": f"f-{cid}", "criteria": [cid]} for cid in routed_ids],
        }
    )

    cov_bespoke: dict = {}
    findings_bespoke = run_pass1(ctx, cfg, fake, single, agent, cov_bespoke)

    # Same criteria as {prompt: id} dicts → the runner re-splits them by exec_tier.
    req = _make_req({"prompt": cid} for cid in routed_ids)
    result = ProductionBatchRunner(runner=fake).run(req, None)

    assert isinstance(result, BatchRunResult)
    assert result.outputs["findings"] == findings_bespoke
    # The test must actually exercise non-empty findings (not a trivial [] == []).
    assert findings_bespoke, "expected the routed criteria to produce findings"


# ── tier split matches exec_tier; unknown ids skipped ──────────────────────────
def test_tier_split_matches_exec_tier_and_skips_unknown(tmp_path, _stub_reads):
    # E2 is a single-turn (1-TURN) criterion; E4 is AGENT-tier.
    assert registry.exec_tier(registry.by_id()["E2"]) != "AGENT"
    assert registry.exec_tier(registry.by_id()["E4"]) == "AGENT"

    fake = FakeRunner(structured={"analysis": "", "findings": []})
    # Pin repo_root to an overlay-free tmp dir so this built-in tier-split assertion is
    # isolated from any activated project.* criterion in the real repo's `.rebar/`
    # overlay (e.g. the dogfood project.portability), which the runner would otherwise
    # fan in via config.repo_root() and add to `single`.
    req = _make_req(
        [{"prompt": "E2"}, {"prompt": "E4"}, {"prompt": "NOPE-not-a-criterion"}],
        repo_root=str(tmp_path),
    )
    result = ProductionBatchRunner(runner=fake).run(req, None)

    res = result.outputs["batch_plan"]["batch_resolution"]
    assert res["single"] == ["E2"]
    assert res["agent"] == ["E4"]
    assert res["skipped"] == ["NOPE-not-a-criterion"]


# ── seam conformance: BatchRunResult + opaque batch_plan + unused agent_runner ──
def test_seam_conformance_and_agent_runner_unused(_stub_reads):
    fake = FakeRunner(structured={"analysis": "", "findings": []})
    req = _make_req([{"prompt": "E2"}])

    # agent_runner is the seam param; the production runner does not use it (D3). Both
    # an explicit None and omitting it entirely must work.
    result_none = ProductionBatchRunner(runner=fake).run(req, None)
    result_omit = ProductionBatchRunner(runner=fake).run(req)

    for result in (result_none, result_omit):
        assert isinstance(result, BatchRunResult)
        # batch_plan IS the coverage dict run_pass1 populates (opaque to the engine).
        plan = result.outputs["batch_plan"]
        assert isinstance(plan, dict)
        assert "chunks" in plan and "budget" in plan  # run_pass1 populated it
        assert result.outputs["criteria_count"] == 1


# ── target_ticket guard ────────────────────────────────────────────────────────
def test_target_ticket_required():
    req = _make_req([{"prompt": "E2"}], target=None)
    with pytest.raises(ValueError, match="target_ticket"):
        ProductionBatchRunner(runner=FakeRunner()).run(req, None)


# ── budget: default computed cap; override journaled but not applied (follow-up) ─
def test_budget_default_computed_cap(_stub_reads):
    from rebar.llm.plan_review import sizing

    ctx = assemble_context(_TARGET, repo_root=None)
    expected_cap = sizing.plan_budget_cap(ctx)

    fake = FakeRunner(structured={"analysis": "", "findings": []})
    req = _make_req([{"prompt": "E2"}, {"prompt": "E4"}])  # usd_budget=None
    result = ProductionBatchRunner(runner=fake).run(req, None)

    plan = result.outputs["batch_plan"]
    assert plan["budget"]["cap_usd"] == expected_cap
    assert "requested_usd_budget" not in plan  # no override requested


def test_budget_override_is_journaled_but_not_yet_applied(_stub_reads):
    # Documents the D4 follow-up: req.usd_budget is recorded, but the computed cap is
    # still used (no clean override seam in pass1/sizing yet).
    from rebar.llm.plan_review import sizing

    ctx = assemble_context(_TARGET, repo_root=None)
    expected_cap = sizing.plan_budget_cap(ctx)

    fake = FakeRunner(structured={"analysis": "", "findings": []})
    req = _make_req([{"prompt": "E2"}, {"prompt": "E4"}], usd_budget=0.01)
    result = ProductionBatchRunner(runner=fake).run(req, None)

    plan = result.outputs["batch_plan"]
    assert plan["requested_usd_budget"] == 0.01
    assert plan["budget_override_applied"] is False
    assert plan["budget"]["cap_usd"] == expected_cap  # computed cap, not the override


def test_resolve_criteria_excludes_isf_and_dedupes():
    # ISF is fed the linked session log by run_pass1 itself (mirrors route_criteria), so it
    # must NOT enter the rubric-chunk routing — else it would be evaluated twice. Duplicates
    # are collapsed, and missing/unknown ids are dropped (skipped/ignored), never fatal.
    from rebar.llm.plan_review.production_batch_runner import _resolve_criteria

    single, agent, skipped = _resolve_criteria(
        (
            {"prompt": "E2"},
            {"prompt": "E2"},  # duplicate → collapsed
            {"prompt": "ISF"},  # excluded (handled by run_pass1's session-log path)
            {"prompt": "NOPE-not-a-criterion"},  # unknown → skipped
            {"with": {}},  # malformed (no prompt id) → ignored
        )
    )
    routed_ids = [d["id"] for d in single + agent]
    assert "ISF" not in routed_ids, "ISF must not be routed as a rubric chunk"
    assert routed_ids.count("E2") == 1, "duplicate criterion ids must be collapsed"
    assert "NOPE-not-a-criterion" in skipped


# ── AC2 sizing behaviours exercised THROUGH the runner, equivalent to run_pass1 ──
# These three tests close the verifier-flagged gap: prove that size-ladder escalation,
# shed-to-budget ordering, and checkpoint resume are driven by ProductionBatchRunner
# (not only by the bespoke orchestrator) AND that the journaled batch_plan coverage is
# IDENTICAL to what the shared run_pass1 path produces for the same scenario.

_IDS_RE = re.compile(r"\(ids: ([^)]*)\)")


def _chunk_ids(req) -> list[str]:  # noqa: ANN001
    """The criterion ids in a Pass-1 finder request, parsed from the rubric header
    ``pass1_chunk`` writes (``(ids: E2, F1, …)``). Lets a fake runner branch on REQUEST
    CONTENT (the chunk size) rather than call ORDER — so its behaviour is deterministic
    under run_pass1's ThreadPoolExecutor."""
    m = _IDS_RE.search(req.instructions)
    assert m, f"could not find the criterion-id header in: {req.instructions!r}"
    return [s.strip() for s in m.group(1).split(",") if s.strip()]


class _BatchContextLimitRunner:
    """Raises a context-limit-shaped error on any MULTI-criterion (batch) call and
    returns one finding per criterion on SINGLE-criterion calls. So run_pass1's size
    ladder falls a batched chunk back to one-criterion-per-call (the escalation), while
    the per-criterion retries then succeed."""

    name = "batch-ctx-limit"

    def preflight(self) -> None:  # pragma: no cover - trivial
        pass

    def run(self, req) -> dict:  # noqa: ANN001
        ids = _chunk_ids(req)
        if len(ids) > 1:
            raise RuntimeError("prompt is too long: exceeds the maximum context window")
        cid = ids[0]
        return {"findings": [{"finding": f"f-{cid}", "criteria": [cid]}]}


class _AllContextLimitRunner:
    """Raises a context-limit-shaped error on EVERY call (batch and single, all models)
    — so each criterion exhausts the model ladder and run_pass1 emits the terminal
    too-big failure finding."""

    name = "all-ctx-limit"

    def preflight(self) -> None:  # pragma: no cover - trivial
        pass

    def run(self, req) -> dict:  # noqa: ANN001
        raise RuntimeError("maximum context length exceeded")


def test_size_ladder_escalation_through_runner_equals_bespoke(_stub_reads):
    ctx = assemble_context(_TARGET, repo_root=None)
    single, agent = route_criteria(ctx)
    cfg = _cfg()
    routed_ids = [c["id"] for c in single + agent]

    # ── batch → one-criterion-per-call fallback (the size-ladder escalation) ──
    fake = _BatchContextLimitRunner()
    cov_bespoke: dict = {}
    findings_bespoke = run_pass1(ctx, cfg, fake, single, agent, cov_bespoke)

    req = _make_req({"prompt": cid} for cid in routed_ids)
    result = ProductionBatchRunner(runner=fake).run(req, None)
    plan = result.outputs["batch_plan"]

    # The runner journaled the ladder escalation, and it records the batch fallback.
    assert "size_ladder" in plan, "the runner must journal the size-ladder events"
    assert any("one-criterion-per-call" in e for e in plan["size_ladder"]), (
        "the multi-criterion batch should fall back to one-criterion-per-call"
    )
    # Equivalence: same ladder trace + same findings as the bespoke run_pass1 path
    # (the runner delegates to the same run_pass1). size_ladder is appended from worker
    # threads, so compare order-insensitively.
    assert sorted(plan["size_ladder"]) == sorted(cov_bespoke["size_ladder"])
    assert result.outputs["findings"] == findings_bespoke
    assert findings_bespoke, "expected the per-criterion retries to produce findings"

    # ── too-big terminal: a criterion that context-limits at every model ──
    fake_all = _AllContextLimitRunner()
    cov_bespoke_tb: dict = {}
    findings_bespoke_tb = run_pass1(ctx, cfg, fake_all, single, agent, cov_bespoke_tb)

    result_tb = ProductionBatchRunner(runner=fake_all).run(req, None)
    plan_tb = result_tb.outputs["batch_plan"]

    assert any("too big" in e for e in plan_tb["size_ladder"]), (
        "exhausting the model ladder should record a too-big terminal event"
    )
    assert all(f["_too_big"] for f in result_tb.outputs["findings"]), (
        "every criterion should yield a too-big failure finding"
    )
    assert sorted(plan_tb["size_ladder"]) == sorted(cov_bespoke_tb["size_ladder"])
    assert result_tb.outputs["findings"] == findings_bespoke_tb


def test_shed_to_budget_ordering_through_runner_equals_bespoke(_stub_reads, monkeypatch):
    # A zero base cap forces every AGENT/overlay criterion to be shed; the shed ORDER
    # is lowest-priority-first — overlays (T*) before the core code-grounding set.
    monkeypatch.setenv("REBAR_PLAN_REVIEW_BUDGET", "0")

    ctx = assemble_context(_TARGET, repo_root=None)
    single, agent = route_criteria(ctx)
    cfg = _cfg()
    routed_ids = [c["id"] for c in single + agent]
    # Pre-condition: this scenario actually mixes overlay + core AGENT criteria so the
    # ordering assertion is meaningful (else it would pass vacuously).
    assert any(registry.is_overlay(c["id"]) for c in agent)
    assert any(not registry.is_overlay(c["id"]) for c in agent)

    fake = FakeRunner(structured={"analysis": "", "findings": []})
    cov_bespoke: dict = {}
    run_pass1(ctx, cfg, fake, single, agent, cov_bespoke)

    req = _make_req({"prompt": cid} for cid in routed_ids)
    result = ProductionBatchRunner(runner=fake).run(req, None)
    shed = result.outputs["batch_plan"]["budget"]["shed"]

    assert shed, "expected agent/overlay criteria to be shed under a zero cap"
    # Lowest-priority-first: every overlay shed precedes every core (non-overlay) shed.
    overlay_pos = [i for i, cid in enumerate(shed) if registry.is_overlay(cid)]
    core_pos = [i for i, cid in enumerate(shed) if not registry.is_overlay(cid)]
    assert overlay_pos and core_pos, "scenario must shed both overlay and core criteria"
    assert max(overlay_pos) < min(core_pos), f"overlays must shed before core: {shed}"
    # Equivalence: identical shed list (same order) as the bespoke run_pass1 path.
    assert shed == cov_bespoke["budget"]["shed"]


def test_checkpoint_resume_through_runner_equals_bespoke(_stub_reads, tmp_path):
    runner_root = tmp_path / "runner"
    bespoke_root = tmp_path / "bespoke"
    runner_root.mkdir()
    bespoke_root.mkdir()

    fake = FakeRunner(structured={"analysis": "", "findings": []})
    # Routing does not depend on repo_root (it only anchors the checkpoint cache dir).
    single0, agent0 = route_criteria(assemble_context(_TARGET, repo_root=None))
    routed_ids = [c["id"] for c in single0 + agent0]
    req = _make_req(({"prompt": cid} for cid in routed_ids), repo_root=str(runner_root))

    # First run populates the per-ticket checkpoint cache (nothing to resume yet).
    first = ProductionBatchRunner(runner=fake).run(req, None)
    cp1 = first.outputs["batch_plan"]["checkpoint"]
    assert cp1["chunks_resumed"] == 0
    assert cp1["chunks_total"] > 0

    # Second run with the same target/material RESUMES every completed chunk.
    second = ProductionBatchRunner(runner=fake).run(req, None)
    cp2 = second.outputs["batch_plan"]["checkpoint"]
    assert cp2["chunks_resumed"] > 0
    assert cp2["chunks_resumed"] == cp2["chunks_total"] == cp1["chunks_total"]

    # Equivalence: the bespoke run_pass1 path, run twice in its OWN cache dir, produces
    # an identical checkpoint record (same material fingerprint, same chunking).
    ctx_b = assemble_context(_TARGET, repo_root=str(bespoke_root))
    single, agent = route_criteria(ctx_b)
    cfg = _cfg()
    cov_b1: dict = {}
    run_pass1(ctx_b, cfg, fake, single, agent, cov_b1)
    cov_b2: dict = {}
    run_pass1(ctx_b, cfg, fake, single, agent, cov_b2)
    assert cov_b2["checkpoint"] == cp2
