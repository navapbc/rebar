"""Pass-1 per-call usage threading (story d52a-40cd-75d4-46cb).

The runner attaches per-call ``_usage`` to every result; these tests prove Pass-1 no
longer discards it: the pass functions return ``(findings, usage)`` pairs, the ladder /
container / ISF / prerequisite paths mint per-call records, ``run_pass1`` aggregates
them into ``coverage["usage"]``, the ``ProductionBatchRunner`` emits the aggregate as
``outputs["_usage"]``, ``_attach_plan_review_metrics`` folds the token totals into
``coverage.metrics``, and the sidecar payload carries
``coverage.usage.per_call`` / ``coverage.usage.per_criterion``.

OFFLINE: ``FakeRunner.run`` does not emit ``_usage``, so a thin TEST-LOCAL wrapper
runner delegates to ``FakeRunner`` and attaches deterministic stub ``_usage`` dicts —
no change to ``FakeRunner`` itself.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import pass1, passes, prerequisites, registry, sizing
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.plan_review.production_batch_runner import ProductionBatchRunner
from rebar.llm.runner import FakeRunner
from rebar.llm.workflow.plan_review_recovery import _attach_plan_review_metrics
from rebar.llm.workflow.runners import BatchRunRequest

_USAGE = {
    "requests": 2,
    "input_tokens": 100,
    "output_tokens": 10,
    "cache_read_tokens": 5,
    "cache_write_tokens": 1,
}

_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


class UsageStubRunner:
    """Test-local wrapper: delegates each call to a ``FakeRunner`` built for the
    request's ``output_schema`` and attaches a deterministic stub ``_usage`` dict to
    the result (``FakeRunner`` itself unchanged)."""

    name = "usage-stub"

    def __init__(self, findings: list[dict] | None = None, usage: dict | None = None) -> None:
        self._findings = findings or []
        self._usage = dict(usage or _USAGE)
        self.calls = 0

    def preflight(self) -> None:  # pragma: no cover - trivial
        pass

    def run(self, req):
        self.calls += 1
        if req.output_schema == "plan_review_prerequisite_coverage":
            # One "consistent" record per prerequisite id in the rendered instructions.
            import re

            ids = re.findall(r'<prerequisite id="([^"]+)">', req.instructions)
            payload = {
                "records": [
                    {"prerequisite_id": pid, "disposition": "consistent", "findings": []}
                    for pid in ids
                ]
            }
        else:
            payload = {"analysis": "", "findings": list(self._findings)}
        result = FakeRunner(structured=payload).run(req)
        result["_usage"] = dict(self._usage)
        return result


class _SeqRunner:
    """Yields each outcome in turn: an Exception is raised, a dict is returned."""

    name = "seq"

    def __init__(self, outcomes: list) -> None:
        self._outcomes = list(outcomes)

    def preflight(self) -> None:  # pragma: no cover - trivial
        pass

    def run(self, req):
        item = self._outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return dict(item)


def _cfg() -> LLMConfig:
    return LLMConfig(runner="fake")


def _ctx(**kw) -> PlanContext:
    defaults = dict(
        ticket_id="d52a-0000-0000-0001",
        ticket_type="task",
        title="A task",
        description="## Acceptance Criteria\n- [ ] the widget is observably correct\n",
    )
    defaults.update(kw)
    return PlanContext(**defaults)


# ── pass functions return (findings, usage) pairs ─────────────────────────────────
def test_pass1_chunk_returns_usage_pair() -> None:
    fr = UsageStubRunner(findings=[{"finding": "x", "criteria": ["E2"]}])
    findings, usage = passes.pass1_chunk(fr, _cfg(), plan="p", chunk=[{"id": "E2", "name": "x"}])
    assert [f["finding"] for f in findings] == ["x"]
    assert usage == _USAGE


def test_pass1_chunk_missing_usage_degrades_to_empty() -> None:
    # FakeRunner attaches no _usage → the pair's usage is {} → usage_record zeros.
    fr = FakeRunner(structured={"analysis": "", "findings": []})
    _findings, usage = passes.pass1_chunk(fr, _cfg(), plan="p", chunk=[{"id": "E2", "name": "x"}])
    assert usage == {}
    rec = sizing.usage_record(["E2"], usage)
    assert rec["requests"] == 0
    assert all(rec[f] == 0 for f in _TOKEN_FIELDS)


def test_pass1_isf_and_summarizer_return_usage() -> None:
    fr = UsageStubRunner()
    findings, usage = passes.pass1_isf(fr, _cfg(), plan="p", session_log_text="log")
    assert findings == [] and usage == _USAGE
    # summarize_for_isf is mode="text": FakeRunner yields no "text" key, but the pair
    # contract (text, usage) holds and usage rides along.
    text, s_usage = passes.summarize_for_isf(fr, _cfg(), log_text="long log")
    assert isinstance(text, str) and s_usage == _USAGE


# ── attribution: multi-criterion split; llm_calls derived ─────────────────────────
def test_multi_criterion_chunk_splits_tokens_and_counts_calls_in_full() -> None:
    agg = pass1.aggregate_usage([sizing.usage_record(["E2", "E5"], _USAGE)])
    for cid in ("E2", "E5"):
        slot = agg["per_criterion"][cid]
        assert slot["input_tokens"] == 50  # 100 split equally across 2 criteria
        assert slot["llm_calls"] == 1  # derived count — in full per criterion
        assert slot["requests"] == 2  # requests count in full per criterion
    assert agg["totals"]["input_tokens"] == 100
    assert agg["totals"]["llm_calls"] == 1
    # Per-call records carry NO llm_calls field (one record IS one call).
    assert "llm_calls" not in agg["per_call"][0]


# ── the size ladder: completed attempts append records; raising attempts do not ───
def test_ladder_completed_attempts_each_append_a_record() -> None:
    ctx_err = Exception("prompt is too long")
    runner = _SeqRunner(
        [
            ctx_err,  # the batch call raises (context limit) → contributes NO record
            {"findings": [{"finding": "a", "criteria": ["E2"]}], "_usage": dict(_USAGE)},
            {"findings": [{"finding": "b", "criteria": ["E5"]}], "_usage": dict(_USAGE)},
        ]
    )
    out, calls = sizing.pass1_with_ladder(
        runner, _cfg(), "plan", [{"id": "E2"}, {"id": "E5"}], False, []
    )
    assert sorted(f["finding"] for f in out) == ["a", "b"]
    assert [c["criteria"] for c in calls] == [["E2"], ["E5"]]  # 2 completed attempts
    assert all(c["input_tokens"] == 100 for c in calls)


def test_ladder_raising_attempt_contributes_no_record() -> None:
    out, calls = sizing.pass1_with_ladder(
        runner=_SeqRunner([Exception("boom, unrelated")]),
        cfg=_cfg(),
        plan="plan",
        chunk=[{"id": "E2"}],
        agentic=False,
        events=[],
    )
    assert out == [] and calls == []


# ── run_pass1: aggregation into coverage["usage"]; checkpoint contributes nothing ──
def test_run_pass1_records_usage_and_checkpoint_resume_adds_nothing(tmp_path) -> None:
    ctx = _ctx(repo_root=str(tmp_path))
    single = [registry.by_id()["E2"]]
    cov1: dict = {}
    pass1.run_pass1(ctx, _cfg(), UsageStubRunner(), single, [], cov1)
    usage1 = cov1["usage"]
    assert len(usage1["per_call"]) == 1
    assert usage1["totals"]["input_tokens"] == 100
    assert usage1["per_criterion"]["E2"]["llm_calls"] == 1

    # Second run: the chunk is served from the checkpoint — NO LLM call, NO usage.
    cov2: dict = {}
    pass1.run_pass1(ctx, _cfg(), UsageStubRunner(), single, [], cov2)
    assert cov2["checkpoint"]["chunks_resumed"] == 1
    assert cov2["usage"]["per_call"] == []
    assert cov2["usage"]["totals"]["input_tokens"] == 0
    assert cov2["usage"]["totals"]["llm_calls"] == 0


def test_run_pass1_missing_usage_degrades_to_zero_totals() -> None:
    cov: dict = {}
    fr = FakeRunner(structured={"analysis": "", "findings": []})
    pass1.run_pass1(_ctx(), _cfg(), fr, [registry.by_id()["E2"]], [], cov)
    assert len(cov["usage"]["per_call"]) == 1  # the call happened…
    assert cov["usage"]["totals"]["input_tokens"] == 0  # …but contributes zeros


# ── container fan-out: one record per completed pairing ───────────────────────────
def test_container_pairings_append_usage_records() -> None:
    ctx = _ctx(
        ticket_type="epic",
        children=[
            {"ticket_id": "c1", "title": "C1", "description": "tiny"},
            {"ticket_id": "c2", "title": "C2", "description": "tiny"},
        ],
        largest_window_tokens=1_000_000,
    )
    container = [registry.by_id()["G3"], registry.by_id()["G4"]]
    cov: dict = {}
    _findings, calls = pass1._run_container(ctx, _cfg(), UsageStubRunner(), container, cov)
    # Small children pack into one bin → ONE completed pairing → ONE call record
    # carrying the container criteria; its usage is read from pairing_record["usage"].
    assert len(calls) == cov["container"]["pairings_evaluated"] == 1
    assert calls[0]["criteria"] == ["G3", "G4"]
    assert calls[0]["input_tokens"] == 100
    assert cov["container"]["pairings"][0]["usage"] == _USAGE


# ── prerequisite finder: usage is the sum over its internal run_bin calls ─────────
def test_run_focused_finder_sums_usage_over_run_bin_calls() -> None:
    stub = UsageStubRunner()
    blocks = [
        {"canonical_id": "aaaa-0000-0000-0001", "rendered_text": "consistent v2"},
        {"canonical_id": "aaaa-0000-0000-0002", "rendered_text": "consistent v2"},
    ]
    records, findings, usage = prerequisites.run_focused_finder(
        stub, _cfg(), subject_plan="subject", blocks=blocks
    )
    assert len(records) == 2 and findings == []
    assert stub.calls >= 1
    assert usage["requests"] == 2 * stub.calls
    assert usage["input_tokens"] == 100 * stub.calls


# ── ProductionBatchRunner: the aggregate includes chunk + agent + prerequisites ───
@pytest.fixture
def _stub_reads(monkeypatch):
    state = {
        "ticket_id": "abcd-0000-0000-0001",
        "ticket_type": "story",
        "title": "Build X",
        "description": (
            "## Why\nneed X.\n\n## What\nbuild X.\n\n## Scope\nX.\n\n"
            "## Acceptance Criteria\n- [ ] X observably works\n"
        ),
        "deps": [],
    }
    monkeypatch.setattr("rebar._reads.show_ticket", lambda tid, *, repo_root=None: dict(state))
    monkeypatch.setattr("rebar._reads.list_tickets", lambda *, parent=None, repo_root=None: [])
    return state


def test_batch_runner_emits_usage_aggregate_with_prerequisites(tmp_path, _stub_reads) -> None:
    stub = UsageStubRunner()
    req = BatchRunRequest(
        finder="plan-review-finder",
        criteria=({"prompt": "E2"}, {"prompt": "E4"}),  # one single-turn chunk + one agent
        usd_budget=None,
        model_ladder=("claude-opus-4-8",),
        workflow={},
        target_ticket="abcd-0000-0000-0001",
        repo_root=str(tmp_path),  # overlay-free root; also isolates the chunk checkpoints
        run_id="run-1",
        step_id="finders",
        with_inputs={
            "prerequisites": [
                {"canonical_id": "aaaa-0000-0000-0001", "rendered_text": "consistent"}
            ]
        },
    )
    result = ProductionBatchRunner(runner=stub).run(req, None)
    usage = result.outputs["_usage"]
    # One E2 chunk call + one E4 agent call + one merged prerequisite record.
    assert [c["criteria"] for c in usage["per_call"]][-1] == [prerequisites.PREREQUISITE_CRITERION]
    assert len(usage["per_call"]) == 3
    # The aggregate totals equal the sum over the per-call records.
    for f in ("requests", *_TOKEN_FIELDS):
        assert usage["totals"][f] == sum(c[f] for c in usage["per_call"])
    assert usage["totals"]["input_tokens"] == 100 * stub.calls
    assert usage["per_criterion"][prerequisites.PREREQUISITE_CRITERION]["llm_calls"] == 1
    # The journaled batch_plan coverage carries the same aggregate.
    assert result.outputs["batch_plan"]["usage"] == usage


# ── _attach_plan_review_metrics folds token totals + attaches coverage.usage ──────
def _batch_step(usage: dict) -> dict:
    return {
        "status": "succeeded",
        "step_id": "finders",
        "kind": "batch",
        "duration_ms": 5.0,
        "outputs": {"criteria_count": 2, "_usage": usage},
    }


def test_attach_metrics_folds_token_totals_into_coverage_metrics() -> None:
    agg = pass1.aggregate_usage(
        [sizing.usage_record(["E2"], _USAGE), sizing.usage_record(["E4"], _USAGE)]
    )
    verdict: dict = {"coverage": {}}
    rec = SimpleNamespace(steps=[_batch_step(agg)])
    _attach_plan_review_metrics(verdict, rec, total_ms=10.0)
    metrics = verdict["coverage"]["metrics"]
    assert metrics["input_tokens"] == 200
    assert metrics["output_tokens"] == 20
    assert metrics["cache_read_tokens"] == 10
    assert metrics["cache_write_tokens"] == 2
    usage = verdict["coverage"]["usage"]
    assert len(usage["per_call"]) == 2
    assert usage["per_criterion"]["E4"]["input_tokens"] == 100


def test_attach_metrics_without_usage_stays_backward_compatible() -> None:
    verdict: dict = {"coverage": {}}
    rec = SimpleNamespace(
        steps=[
            {
                "status": "succeeded",
                "step_id": "finders",
                "kind": "batch",
                "duration_ms": 5.0,
                "outputs": {"criteria_count": 2},
            }
        ]
    )
    _attach_plan_review_metrics(verdict, rec, total_ms=10.0)
    metrics = verdict["coverage"]["metrics"]
    assert "input_tokens" not in metrics  # no _usage output → no token keys, no crash
    assert "usage" not in verdict["coverage"]


# ── sidecar payload: coverage.usage.per_call / coverage.usage.per_criterion ───────
def test_sidecar_payload_carries_usage_under_documented_keys() -> None:
    from rebar.llm.plan_review import sidecar

    agg = pass1.aggregate_usage([sizing.usage_record(["E2", "E5"], _USAGE)])
    verdict = {
        "verdict": "PASS",
        "ticket_id": "d52a-0000-0000-0001",
        "coverage": {"usage": {"per_call": agg["per_call"], "per_criterion": agg["per_criterion"]}},
    }
    payload = sidecar.build_payload(verdict, material="m")
    usage = payload["coverage"]["usage"]
    assert set(usage) == {"per_call", "per_criterion"}  # the exact documented key names
    assert usage["per_call"] == agg["per_call"]
    assert usage["per_criterion"]["E2"]["input_tokens"] == 50


def test_sidecar_payload_without_usage_has_no_usage_key() -> None:
    from rebar.llm.plan_review import sidecar

    payload = sidecar.build_payload(
        {"verdict": "PASS", "ticket_id": "t", "coverage": {}}, material="m"
    )
    assert "usage" not in payload["coverage"]
