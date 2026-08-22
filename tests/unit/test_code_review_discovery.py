"""RP-06 S3 — cut code review over to the shared effective-policy discovery kernel.

Behavioral oracle for the cutover: the code-review Round-A/Round-B fan-in now compiles a
``DiscoveryStagePlan`` from the effective ``CriteriaSnapshot`` and executes it through the
shared ``review_kernel.execute_stage`` kernel, producing trustworthy per-unit execution FACTS
(success/shed/failed/skipped/cancelled) that drive:

* the internal discovery TRACE recorded in the batch journal (never in the review response);
* explicit-budget shedding BEFORE any model call;
* partial-failure resilience (an exhausted local criterion never erases earlier successes and
  never blocks independent criteria);
* the snapshot-driven selection deltas (effective disable/retune, project applicability, project
  DET) that the shadow-contract comparison allowlists.

The model boundary is faked BELOW the discovery kernel: the fake ``agent_runner`` returns canned
structured output per prompt, or raises to simulate an RP-01 local exhaustion / systemic outage.
Assertions are on observable behavior only — the aggregated findings, the exact per-unit trace
kinds, the journal, and the unchanged public response shape — never private structure.
"""

from __future__ import annotations

import pytest

from rebar.llm.code_review import detectors as _detectors
from rebar.llm.code_review import registry as _registry
from rebar.llm.code_review import sidecar as _sidecar
from rebar.llm.code_review import workflow_ops as _wops
from rebar.llm.code_review.batch_runner import CodeReviewBatchRunner
from rebar.llm.criteria.snapshot import compile_snapshot
from rebar.llm.errors import LLMError, LLMUnavailableError
from rebar.llm.workflow import executor as _ex
from rebar.llm.workflow.runners import BatchRunRequest

pytestmark = pytest.mark.unit


# ── fakes at the model-call boundary ─────────────────────────────────────────────────────────
class _Agent:
    """Canned per-prompt structured output; can raise a LOCAL (LLMError) or SYSTEMIC
    (LLMUnavailableError) failure for a named prompt to drive partial-failure paths."""

    def __init__(self, *, raise_local=(), raise_systemic=()):
        self.calls: list[str] = []
        self._raise_local = set(raise_local)
        self._raise_systemic = set(raise_systemic)

    def run(self, ctx):
        prompt = str(ctx.step["prompt"])
        self.calls.append(prompt)
        if prompt in self._raise_systemic:
            raise LLMUnavailableError(f"provider down for {prompt}")
        if prompt in self._raise_local:
            raise LLMError(f"local op exhausted for {prompt}")
        oid = prompt.replace("code-review-", "")
        return _ex.StepResult(
            outputs={
                "findings": [
                    {
                        "finding": f"{oid} finding",
                        "criteria": [oid],
                        "evidence": [f"{oid}.py:1"],
                        "location": f"{oid}.py:1",
                    }
                ],
                "_usage": {"input_tokens": 5, "output_tokens": 2},
            }
        )


def _crit(prompt: str, criterion_id: str | None = None) -> dict[str, str]:
    entry = {"prompt": prompt}
    if criterion_id is not None:
        entry["criterion_id"] = criterion_id
    return entry


def _req(*, criteria=(), step_id="round_a", usd_budget=None, repo_root=None) -> BatchRunRequest:
    return BatchRunRequest(
        finder="code-review-base",
        criteria=tuple(criteria),
        usd_budget=usd_budget,
        model_ladder=(),
        workflow={},
        target_ticket=None,
        repo_root=repo_root,
        run_id="disc-test",
        step_id=step_id,
    )


def _traces(result) -> dict[str, str]:
    """{unit_id: kind} from the internal discovery trace journal (safe, redacted)."""
    trace = _sidecar.discovery_trace_from_journal(result.outputs["batch_plan"])
    return {t["unit_id"]: t["kind"] for t in trace}


# ══════════════════════════════════════════════════════════════════════════════════════════
# HAPPY PATH — kept in-tree for the held-out implementer.
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_fan_in_runs_each_criterion_once_through_the_kernel():
    agent = _Agent()
    runner = CodeReviewBatchRunner(context="DIFF")
    result = runner.run(
        _req(criteria=[_crit("code-review-security"), _crit("code-review-tests")]), agent
    )
    # every included criterion ran exactly once and contributed its finding (authored order).
    assert result.outputs["criteria_count"] == 2
    assert result.outputs["batch_plan"]["ran"] == ["code-review-security", "code-review-tests"]
    assert [f["finding"] for f in result.outputs["findings"]] == [
        "security finding",
        "tests finding",
    ]
    # each finding is provenance-tagged with its emitting overlay (parity with the pre-cutover
    # runner) and NOTHING leaks the discovery trace into the finding payload.
    for f in result.outputs["findings"]:
        assert f["reviewer_id"].startswith("code-review-")
        assert "discovery_trace" not in f and "envelope" not in f


def test_discovery_trace_is_recorded_internally_with_success_kinds():
    agent = _Agent()
    result = CodeReviewBatchRunner(context="DIFF").run(
        _req(criteria=[_crit("code-review-security"), _crit("code-review-docs")]), agent
    )
    assert _traces(result) == {"code-review-security": "success", "code-review-docs": "success"}
    # the safe trace carries a normalized shape only — never envelope content / prompt bodies.
    for t in _sidecar.discovery_trace_from_journal(result.outputs["batch_plan"]):
        assert set(t) >= {"unit_id", "kind", "usage", "namespace_version"}
        assert "content" not in t and "prompt" not in t


def test_review_response_shape_is_unchanged_by_the_trace():
    # the public batch outputs keep exactly the historical keys; the trace rides in batch_plan.
    result = CodeReviewBatchRunner(context="DIFF").run(
        _req(criteria=[_crit("code-review-security")]), _Agent()
    )
    assert set(result.outputs) == {"findings", "criteria_count", "batch_plan", "_usage"}
    assert "discovery_trace" not in result.outputs


def test_no_criteria_yields_an_empty_but_well_formed_journal():
    result = CodeReviewBatchRunner(context="DIFF").run(_req(criteria=[]), _Agent())
    assert result.outputs["findings"] == []
    assert result.outputs["criteria_count"] == 0
    assert _sidecar.discovery_trace_from_journal(result.outputs["batch_plan"]) == []


def test_round_a_selection_from_snapshot_excludes_disabled_builtins(tmp_path):
    _write_overlay(tmp_path, {"code_review": {"security": {"disabled": True}}, "activate": []})
    snap = compile_snapshot(str(tmp_path))
    sel = _registry.round_a_selection(snap, changed_files=["src/auth.py"])
    assert "security" not in sel["builtins"]  # effective disable honored from ONE snapshot
    assert "tests" in sel["builtins"]  # a non-disabled built-in still selects


# ══════════════════════════════════════════════════════════════════════════════════════════
# EDGE + E2E — HELD OUT from the implementer.
# ══════════════════════════════════════════════════════════════════════════════════════════
def _write_overlay(root, overlay):
    import json

    d = root / ".rebar"
    d.mkdir(parents=True, exist_ok=True)
    (d / "criteria_routing.json").write_text(json.dumps(overlay), encoding="utf-8")


# ── AC4: explicit budget shedding BEFORE calls / uncapped omission / zero rejected ───────────
def test_positive_budget_sheds_over_budget_units_before_any_call():
    agent = _Agent()
    # four unit-cost criteria, budget for ~one; the kernel sheds non-blocking, tie by unit_id
    # descending, so the retained set is the alphabetically-FIRST criterion only.
    crits = [
        _crit("code-review-a11y"),
        _crit("code-review-docs"),
        _crit("code-review-security"),
        _crit("code-review-tests"),
    ]
    budget = 1.5 * CodeReviewBatchRunner.CRITERION_COST_ESTIMATE
    result = CodeReviewBatchRunner(context="DIFF").run(
        _req(criteria=crits, usd_budget=budget), agent
    )
    kinds = _traces(result)
    retained = {u for u, k in kinds.items() if k == "success"}
    shed = {u for u, k in kinds.items() if k == "shed"}
    assert retained == {"code-review-a11y"}  # only the retained unit was DISPATCHED
    assert shed == {"code-review-docs", "code-review-security", "code-review-tests"}
    assert agent.calls == ["code-review-a11y"]  # shed happened BEFORE the calls
    assert result.outputs["criteria_count"] == 1


def test_omitted_budget_stays_uncapped():
    agent = _Agent()
    crits = [_crit(f"code-review-{o}") for o in ("a11y", "docs", "security", "tests")]
    result = CodeReviewBatchRunner(context="DIFF").run(_req(criteria=crits, usd_budget=None), agent)
    assert len(agent.calls) == 4  # nothing shed
    assert all(k == "success" for k in _traces(result).values())


def test_zero_budget_is_rejected_by_workflow_validation():
    with pytest.raises(ValueError, match="budget"):
        CodeReviewBatchRunner(context="DIFF").run(
            _req(criteria=[_crit("code-review-security")], usd_budget=0), _Agent()
        )


# ── AC5: partial failure — retain successes, continue independents, INDETERMINATE policy ─────
def test_one_local_exhaustion_keeps_other_successes_and_marks_that_unit_failed():
    agent = _Agent(raise_local={"code-review-tests"})
    crits = [
        _crit("code-review-security"),
        _crit("code-review-tests"),
        _crit("code-review-docs"),
    ]
    result = CodeReviewBatchRunner(context="DIFF").run(_req(criteria=crits), agent)
    kinds = _traces(result)
    assert kinds["code-review-tests"] == "failed"
    assert kinds["code-review-security"] == "success" and kinds["code-review-docs"] == "success"
    # the two independent successes survive and still contribute findings.
    surviving = {f["finding"] for f in result.outputs["findings"]}
    assert surviving == {"security finding", "docs finding"}
    assert result.outputs["criteria_count"] == 2


def test_systemic_outage_aborts_remaining_units_with_exact_accounting():
    # the FIRST dispatched unit (alphabetically) raises a systemic outage → the rest are skipped;
    # no hollow success is recorded, and usage counts only the (zero) successes.
    agent = _Agent(raise_systemic={"code-review-a11y"})
    crits = [_crit(f"code-review-{o}") for o in ("a11y", "docs", "security")]
    result = CodeReviewBatchRunner(context="DIFF").run(_req(criteria=crits), agent)
    kinds = _traces(result)
    assert kinds["code-review-a11y"] == "failed"
    assert kinds["code-review-docs"] == "skipped" and kinds["code-review-security"] == "skipped"
    assert result.outputs["findings"] == []
    assert result.outputs["criteria_count"] == 0
    assert result.outputs["_usage"]["input_tokens"] == 0  # no success ⇒ no usage


def test_partial_failure_verdict_policy_indeterminate_vs_block_vs_advisory_pass():
    # a real blocking finding WINS block even with a gap ...
    v = _wops.partial_failure_verdict(
        {"verdict": "PASS", "blocking": [{"x": 1}]},
        blocking_capable_gap=True,
        real_block=True,
    )
    assert v["verdict"] == "BLOCK"
    # ... missing BLOCKING-CAPABLE coverage with no real block ⇒ unsigned INDETERMINATE ...
    v = _wops.partial_failure_verdict(
        {"verdict": "PASS", "blocking": []}, blocking_capable_gap=True, real_block=False
    )
    assert v["verdict"] == "INDETERMINATE"
    # ... advisory-only missing coverage may still PASS.
    v = _wops.partial_failure_verdict(
        {"verdict": "PASS", "blocking": []}, blocking_capable_gap=False, real_block=False
    )
    assert v["verdict"] == "PASS"


# ── AC7: shadow contract — one call set, differences only in the approved delta allowlist ─────
def test_shadow_compare_reports_only_differences_outside_the_allowlist():
    legacy = ["security", "tests", "docs"]
    # the new snapshot-driven selection drops `security` (an effective DISABLE) and adds
    # `project.foo` (an activated project criterion) — both approved deltas ⇒ no reported diff.
    new = ["tests", "docs", "project.foo"]
    diff = _wops.shadow_compare(legacy, new, allowlist={"security", "project.foo"})
    assert diff == {"added": [], "removed": []}
    # an UNAPPROVED drift (a11y silently added) is reported.
    diff = _wops.shadow_compare(legacy, [*new, "a11y"], allowlist={"security", "project.foo"})
    assert diff == {"added": ["a11y"], "removed": []}


def test_shadow_compare_uses_one_observed_call_set_no_duplicate_calls():
    # the same observed dispatch set feeds BOTH projections; the runner made exactly one call
    # per retained criterion (no second model pass to compute the legacy projection).
    agent = _Agent()
    result = CodeReviewBatchRunner(context="DIFF").run(
        _req(criteria=[_crit("code-review-security"), _crit("code-review-tests")]), agent
    )
    observed = list(result.outputs["batch_plan"]["ran"])
    assert sorted(agent.calls) == sorted(observed)  # one call set, reused
    diff = _wops.shadow_compare(observed, observed, allowlist=set())
    assert diff == {"added": [], "removed": []}


# ── AC2: activated project DET fixtures drive selection from ONE snapshot ────────────────────
def test_project_det_selection_comes_from_the_snapshot(tmp_path):
    _write_overlay(
        tmp_path,
        {
            "code_review": {
                "project.inv": {
                    "exec": "DET",
                    "detector": {"id": "project.no-eval"},
                    "fail_mode": "closed",
                    "default_posture": "blocking",
                    "block_threshold": 0.5,
                    "name": "no eval",
                }
            },
            "activate": ["project.inv"],
        },
    )
    snap = compile_snapshot(str(tmp_path))
    det_map = _detectors.det_criteria_from_snapshot(snap)
    assert "project.inv" in det_map
    assert det_map["project.inv"]["fail_mode"] == "closed"
    assert det_map["project.inv"]["detector"] == {"id": "project.no-eval"}
