"""The v3 ``batch`` step: schema, inclusion-by-`when`, the delegating runner seam, the
journaled (opaque) plan, the split-on-context-limit fallback, and v2->v3 migration (A2).

The IR is THIN: a ``batch`` step declares the finder + an authored, prompt-library-backed
``criteria`` list (each with an optional ``when`` overlay) + budget/ladder params; a
batch-RUNNER does the adaptive work and journals an OPAQUE plan the interpreter stores but
never branches on. Epic A ships the reference :class:`DefaultBatchRunner`.
"""

from __future__ import annotations

import pytest

from rebar.llm.workflow import executor as _ex
from rebar.llm.workflow import migrate as _migrate
from rebar.llm.workflow import schema as _schema
from rebar.llm.workflow import steps as _steps  # noqa: F401 — registers overlay_triggers

pytestmark = pytest.mark.unit


# ── schema validation (AC1: bad params are validation errors) ─────────────────────────────
def _validate(doc: dict) -> list[str]:
    return _schema.validate_document(_migrate.migrate_to_current(doc))


def _wf(batch: dict, extra_steps: list | None = None) -> dict:
    return {
        "schema_version": "3",
        "name": "batch-wf",
        "inputs": {"plan": {"type": "string"}},
        "steps": (extra_steps or []) + [{"id": "finders", "batch": batch}],
    }


def test_valid_batch_validates():
    assert (
        _validate(
            _wf(
                {
                    "prompt": "plan-review-finder",
                    "criteria": [{"prompt": "plan-review-E1"}, {"prompt": "plan-review-G6"}],
                    "usd_budget": 3.0,
                    "model_ladder": ["claude-haiku-4-5", "claude-sonnet-4-6"],
                }
            )
        )
        == []
    )


def test_batch_missing_criteria_is_error():
    errs = _validate(_wf({"prompt": "f"}))
    assert any("criteria" in e for e in errs)


def test_batch_bad_usd_budget_is_error():
    errs = _validate(_wf({"prompt": "f", "criteria": [{"prompt": "c"}], "usd_budget": "lots"}))
    assert any("usd_budget" in e for e in errs)


def test_batch_cannot_mix_with_another_discriminator():
    doc = {
        "schema_version": "3",
        "name": "x",
        "steps": [
            {"id": "b", "uses": "noop", "batch": {"prompt": "f", "criteria": [{"prompt": "c"}]}}
        ],
    }
    assert _validate(doc) != []


def test_criterion_rejects_unknown_key():
    errs = _validate(_wf({"prompt": "f", "criteria": [{"prompt": "c", "bogus": 1}]}))
    assert any("bogus" in e or "additional" in e.lower() for e in errs)


# ── v2 -> v3 migration golden + chaining (the new shim) ────────────────────────────────────
def test_v2_doc_upconverts_to_v3():
    v2 = {"schema_version": "2", "name": "x", "steps": [{"id": "a", "uses": "fetch_ticket"}]}
    out = _migrate.migrate_to_current(v2)
    assert out["schema_version"] == "3"
    assert out["steps"] == v2["steps"]  # pure version bump, no structural rewrite


def test_v1_chains_through_to_v3():
    v1 = {"schema_version": "1", "name": "x", "steps": [{"id": "a", "uses": "fetch_ticket"}]}
    assert _migrate.migrate_to_current(v1)["schema_version"] == "3"


def test_v2_to_v3_shim_is_registered():
    assert "2" in _migrate.registered_source_versions()


# ── runtime: inclusion-by-`when`, the journaled plan ──────────────────────────────────────
class _Rec(_ex.RunRecorder):
    def __init__(self):
        self.store: dict = {}
        self.events: list[dict] = []

    def run_started(self, record):
        self.events.append(dict(record))

    def run_finished(self, record): ...

    def step_recorded(self, record):
        self.events.append(dict(record))
        if record.get("status") == "running":
            return
        self.store[record.get("frame_key") or record.get("step_id")] = dict(record)

    def completed_step(self, run_id, frame_key):
        rec = self.store.get(frame_key)
        return rec if rec and rec.get("status") == "succeeded" else None


def _run(plan_text: str, *, agent_runner=None, batch_runner=None):
    wf = _wf(
        {
            "prompt": "plan-review-finder",
            "criteria": [
                {"prompt": "plan-review-E1"},
                {"prompt": "plan-review-T5c", "when": "${{ steps.t.outputs.security }}"},
                {"prompt": "plan-review-G6"},
            ],
        },
        extra_steps=[
            {
                "id": "t",
                "uses": "overlay_triggers",
                "with": {
                    "text": "${{ inputs.plan }}",
                    "keyword_triggers": {"security": ["secret"]},
                },
            }
        ],
    )
    wf["steps"][-1]["needs"] = ["t"]
    rec = _Rec()
    _ex.run_workflow(
        wf,
        {"plan": plan_text},
        recorder=rec,
        scripted_registry=dict(_ex.STEP_REGISTRY),
        agent_runner=agent_runner or _ex.FakeAgentRunner(),
        batch_runner=batch_runner,
    )
    return rec.store["finders"]


def test_conditional_criterion_included_when_trigger_truthy():
    out = _run("this plan stores a secret")["outputs"]
    assert "plan-review-T5c" in out["included"]
    assert out["skipped"] == []


def test_conditional_criterion_skipped_when_trigger_falsy():
    out = _run("a clean plan")["outputs"]
    assert "plan-review-T5c" not in out["included"]
    assert out["skipped"] == ["plan-review-T5c"]
    # The skipped criterion is not in any journaled batch.
    batched = [cid for b in out["batch_plan"]["batches"] for cid in b["criteria"]]
    assert "plan-review-T5c" not in batched


def test_batch_journals_a_plan():
    out = _run("contains a secret")["outputs"]
    plan = out["batch_plan"]
    assert plan["finder"] == "plan-review-finder"
    assert plan["enforced"] is False  # reference runner does not enforce budget (epic B does)
    assert all(b["outcome"] in ("ran", "split") for b in plan["batches"])


# ── the split-on-context-limit fallback (the one real adaptive path in the reference) ─────
class _SplitOnBigBatch(_ex.AgentStepRunner):
    """A fake finder that signals a context limit for any batch of >1 criterion, forcing the
    reference runner to split until each call carries a single criterion."""

    def run(self, ctx):
        n = len(ctx.inputs.get("criteria") or [])
        return _ex.StepResult(outputs={"findings": [], "_context_limit": n > 1}, status="succeeded")


def test_runner_splits_a_batch_on_context_limit():
    out = _run("contains a secret", agent_runner=_SplitOnBigBatch())["outputs"]
    batches = out["batch_plan"]["batches"]
    assert any(b["outcome"] == "split" for b in batches), "a context-limit must force a split"
    # Every batch that actually RAN carries exactly one criterion (split down to singletons).
    assert all(len(b["criteria"]) == 1 for b in batches if b["outcome"] == "ran")


# ── AC3: the interpreter stores the plan but NEVER branches on its internals ──────────────
class _GarbagePlanRunner(_ex.BatchRunner):
    """A runner whose journaled plan is intentionally malformed/opaque. If the interpreter
    inspected plan internals to drive control flow, this would break it; it must not."""

    def run(self, req, agent_runner):
        return _ex.BatchRunResult(
            outputs={
                "findings": [{"x": 1}],
                "batch_plan": {"this": ["is", {"opaque": True}], 7: None},
            }
        )


def test_interpreter_does_not_branch_on_plan_internals():
    step = _run("contains a secret", batch_runner=_GarbagePlanRunner())
    # The step still succeeds and the opaque plan is stored verbatim — the interpreter copied
    # the runner's outputs wholesale and never read the plan to decide anything.
    assert step["status"] == "succeeded"
    assert step["outputs"]["batch_plan"] == {"this": ["is", {"opaque": True}], 7: None}
    assert step["outputs"]["findings"] == [{"x": 1}]
