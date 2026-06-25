"""A3 de-risking experiment: the disposable 4-pass review skeleton runs OFFLINE on the v3
engine, and a PLANNED TRACE is captured — proving epic B's parity-validation mechanism is
emittable from the engine before B depends on it.

Also pins the two flaws this experiment SURFACED + FIXED in epic A:
 * the linter now existence-checks a `batch` step's nested prompts (finder + criteria), which
   the agent-step path missed (a gap in the A2 construct);
 * (recorded for epic B, not fixed here) the plan-review criterion prompts are not yet in the
   workflow prompt catalog, so the skeleton uses catalog ids — the criteria-library<->prompt-
   library wiring is a B prerequisite.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from rebar.llm.workflow import executor as _ex
from rebar.llm.workflow import lint as _lint
from rebar.llm.workflow import migrate as _migrate
from rebar.llm.workflow import schema as _schema
from rebar.llm.workflow import steps as _steps  # noqa: F401 — registers overlay_triggers
from rebar.llm.workflow.trace import PlannedTraceRunner

pytestmark = pytest.mark.unit

_SKELETON = pathlib.Path("src/rebar/llm/workflow/examples/review_skeleton.yaml")


def _doc() -> dict:
    return _migrate.migrate_to_current(yaml.safe_load(_SKELETON.read_text()))


class _Rec(_ex.RunRecorder):
    def __init__(self):
        self.store: dict = {}

    def run_started(self, record): ...
    def run_finished(self, record): ...

    def step_recorded(self, record):
        if record.get("status") == "running":
            return
        self.store[record.get("frame_key") or record.get("step_id")] = dict(record)

    def completed_step(self, run_id, frame_key):
        rec = self.store.get(frame_key)
        return rec if rec and rec.get("status") == "succeeded" else None


# ── the skeleton is a valid, lint-clean v3 workflow ───────────────────────────────────────
def test_skeleton_validates_and_lints():
    doc = _doc()
    assert doc["schema_version"] == "3"
    assert _schema.validate_document(doc) == []
    # lint_workflow is the FULL linter (schema + references + prompt-existence + secrets) — it
    # exercises the prompt-ref checks (incl. the new batch nested-prompt check) on real text.
    findings = [
        str(f) for f in _lint.lint_workflow(_SKELETON.read_text()) if f.severity != "warning"
    ]
    assert findings == [], findings


# ── it runs end-to-end OFFLINE; a planned trace is captured; zero live calls ──────────────
def _run(plan_text: str):
    rec = _Rec()
    tracer = PlannedTraceRunner()  # inner FakeAgentRunner → offline, no tokens
    _ex.run_workflow(
        _doc(),
        {"plan": plan_text},
        recorder=rec,
        scripted_registry=dict(_ex.STEP_REGISTRY),
        agent_runner=tracer,
    )
    return rec, tracer


def test_skeleton_runs_offline_with_conditional_inclusion_and_journaled_plan():
    rec, tracer = _run("the design persists a secret credential to disk")
    # All four passes ran; the security criterion was INCLUDED (the plan trips the trigger).
    assert rec.store["triggers"]["status"] == "succeeded"
    finders = rec.store["finders"]["outputs"]
    assert "security" in finders["included"] and finders["skipped"] == []
    assert finders["batch_plan"]["finder"] == "code-quality"  # the journaled plan exists
    assert rec.store["verify"]["status"] == "succeeded"
    assert rec.store["decide"]["status"] == "succeeded"


def test_security_criterion_excluded_when_plan_is_clean():
    rec, _ = _run("a clean, unremarkable plan with nothing sensitive")
    assert rec.store["finders"]["outputs"]["skipped"] == ["security"]


def test_planned_trace_is_captured_offline():
    _rec, tracer = _run("stores a secret token")
    trace = tracer.trace
    # Every captured call carries the parity fields B compares: prompt + intended model +
    # mode + call-mode, with no live calls made (the inner runner is the fake).
    assert trace, "the run must issue at least one (planned) LLM call"
    for entry in trace:
        assert set(entry) >= {"prompt", "model", "mode", "call_mode", "criteria"}
    prompts = [e["prompt"] for e in trace]
    assert "code-quality" in prompts  # the batch finder
    assert "completion-verifier" in prompts  # the aggregate verify
    # The finder calls carry the BATCHED criteria (the prompt-library ids of the included set);
    # the security criterion appears because the plan tripped its trigger.
    batched = {c for e in trace for c in e["criteria"]}
    assert {"ticket-quality", "tests", "security"} <= batched


# ── the FLAW this experiment fixed: the linter now checks a batch step's nested prompts ───
def test_linter_now_flags_an_unknown_batch_prompt():
    doc = {
        "schema_version": "3",
        "name": "bad-batch",
        "steps": [
            {
                "id": "finders",
                "batch": {
                    "prompt": "no-such-finder",
                    "criteria": [{"prompt": "ticket-quality"}, {"prompt": "no-such-criterion"}],
                },
            }
        ],
    }
    findings = [str(f) for f in _lint.lint_prompt_refs(_migrate.migrate_to_current(doc))]
    assert any("no-such-finder" in f for f in findings)
    assert any("no-such-criterion" in f for f in findings)
