"""Per-step attribution in the usage log, and per-step models BENEATH the runner (story b690).

Two things are asserted from ONE real gate run, because both need the same expensive setup — the
actual `code-review.yaml` document, the real interpreter, the real `RunnerAgentStep`, and a real
`PydanticAIRunner` (its model replaced by an offline `FunctionModel`, so no billable call):

 1. **The usage log attributes each call to a STEP.** `record()` used to write only `op` — the
    PROMPT name — which does not identify the step. MEASURED here rather than assumed: the batch
    runner dispatches each overlay under the overlay's OWN prompt, not under the `batch.prompt`
    finder, and both batch blocks declare the SAME overlay prompt list — so an overlay row's `op`
    cannot say which pass ran it. Asserted from a log an actual run produced, never by reading
    `usage_log.py`: reading the writer cannot show that the step id ARRIVES.
 2. **Each step's declared model class reaches the model resolution point.** The sibling test
    `test_code_review_step_model_attribution.py` stops one layer higher, at the value
    `RunnerAgentStep` hands the runner; that layer was always correct. Here the observation is
    `_pai_model`'s argument — the value the ProviderSession, capability record and fallback chain
    are all built from — which is what the runner had been taking from its own shared config while
    discarding the request's.

Note on the recorded `model` field: with a `model_override` the runner deliberately stamps
`test:FunctionModel` as `ran_model` (provenance must show that a double ran), so the per-step MODEL
is asserted from the `_pai_model` spy rather than from the log. The log is asserted for the two
fields that are independent of the override: `step` and `model_class`.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import replace

import pytest

from rebar.llm import anthropic_model as anthropic_model_mod

pytest.importorskip("pydantic_ai")

import yaml
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from rebar.llm import gate_context, usage_log
from rebar.llm.code_review.batch_runner import CodeReviewBatchRunner
from rebar.llm.config import LLMConfig
from rebar.llm.runner import PydanticAIRunner
from rebar.llm.workflow import executor as _ex
from rebar.llm.workflow import migrate as _migrate
from rebar.llm.workflow import steps as _steps  # noqa: F401 — registers the code_review ops
from rebar.llm.workflow.runs import RunnerAgentStep

pytestmark = pytest.mark.unit

_GATE = pathlib.Path("src/rebar/llm/workflow/gates/code-review.yaml")

_CFG_MODEL = "anthropic:claude-opus-4-8"  # also what an unconfigured `frontier` resolves to
_FRONTIER = "test:frontier-model"
_STANDARD = "test:standard-model"
_TRIVIAL = "test:trivial-model"

_ENV_VARS = tuple(
    f"REBAR_LLM_{cls}_{field}"
    for cls in ("TRIVIAL", "STANDARD", "FRONTIER")
    for field in ("MODEL", "PROVIDER", "ENDPOINT")
)

_DIFF = "diff --git a/src/auth/login.py b/src/auth/login.py\n+++ b/src/auth/login.py\n+x\n"

_FINDING = {
    "finding": "note",
    "criteria": ["correctness"],
    "evidence": ["a.py:1"],
    "location": "a.py:1",
}


# One payload that satisfies every schema in this gate at once — the UNION of the four output
# shapes. Deliberately not keyed on the step: keying the fake's response off the step id would make
# the model assertions partly self-referential, since the step id is itself one of the things under
# test. Extra keys are ignored by each step's own schema, so a superset is both simpler and more
# honest here. The CONTENT is irrelevant to every assertion; only step ids and models are asserted.
_PAYLOAD = {
    "findings": [_FINDING],
    # Escalates two overlays, which is what gives Round-B a non-empty membership so it runs at all.
    "recommend_overlays": [
        {"overlay_id": "tests", "reason": "escalate"},
        {"overlay_id": "performance", "reason": "escalate"},
    ],
    "verifications": [],
    "notes": [],
}


def _offline_model(messages, info: AgentInfo) -> ModelResponse:
    """Answer immediately with the step's structured output — never enter a tool loop.

    Both output paths are covered because which one is used is a CAPABILITY decision, not a fixed
    one: the sentinel class models here resolve to the conservative capability record ("no native
    structured output"), so the stack uses PromptedOutput and parses the final text — but a
    configuration whose models have a known profile would take the output-tool path instead.
    """
    if info.output_tools:
        return ModelResponse(
            parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=_PAYLOAD)]
        )
    return ModelResponse(parts=[TextPart(json.dumps(_PAYLOAD))])


class _Rec(_ex.RunRecorder):
    def __init__(self) -> None:
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


@pytest.fixture
def gate_run(tmp_path, monkeypatch):
    """Run the REAL gate once offline; return (usage-log rows, [(step, resolved model), ...])."""
    from rebar.llm import config as llm_config

    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("REBAR_LLM_MODEL", raising=False)
    # A class table whose values all DIFFER from cfg.model, so "honoured the declaration" and
    # "fell through to cfg.model" are distinguishable strings rather than the same one. (With no
    # table configured, `frontier` resolves to the built-in opus default — which IS cfg.model —
    # and the observation would carry no information.)
    monkeypatch.setattr(
        llm_config,
        "_read_llm_file_table",
        lambda repo_root=None: {
            "model_classes": {
                "trivial": {"model": _TRIVIAL},
                "standard": {"model": _STANDARD},
                "frontier": {"model": _FRONTIER},
            }
        },
    )

    log_path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("REBAR_USAGE_LOG", str(log_path))

    resolved: list[tuple[str | None, str]] = []
    real_pai_model = anthropic_model_mod._pai_model

    def _spy(cfg):
        active = usage_log.active_step()
        resolved.append((active[0] if active else None, cfg.model))
        return real_pai_model(cfg)

    monkeypatch.setattr(anthropic_model_mod, "_pai_model", _spy)

    doc = _migrate.migrate_to_current(yaml.safe_load(_GATE.read_text()))
    cfg = replace(
        LLMConfig.from_env(), runner="pydantic_ai", repo_path=".", model=_CFG_MODEL, api_key=None
    )
    runner = PydanticAIRunner(cfg, model_override=FunctionModel(_offline_model))
    with gate_context.gate_session(), gate_context.use_code_root("."):
        _ex.run_workflow(
            doc,
            {"base": "HEAD~1", "head": "HEAD", "diff_text": _DIFF, "changed_files": []},
            recorder=_Rec(),
            scripted_registry=dict(_ex.STEP_REGISTRY),
            agent_runner=RunnerAgentStep(runner=runner, config=cfg),
            batch_runner=CodeReviewBatchRunner(context="## Diff\n(fake)"),
        )
    rows = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    return rows, resolved


def _models_by_step(pairs) -> dict[str | None, set[str]]:
    out: dict[str | None, set[str]] = {}
    for step, model in pairs:
        out.setdefault(step, set()).add(model)
    return out


# ── the usage log attributes each call to a step (ACs 1-2) ──────────────────────────────────


def test_usage_log_records_the_step_that_made_each_call(gate_run):
    rows, _ = gate_run
    assert rows, "the gate produced no usage records — the harness is not exercising the runner"
    assert all("step" in row for row in rows), (
        f"a usage record carries no step id: {[r for r in rows if 'step' not in r]}"
    )


def test_usage_log_distinguishes_the_two_batch_passes(gate_run):
    """`op` cannot tell Round-A work from Round-B work, so `step` has to.

    MEASURED rather than assumed: the batch runner dispatches each overlay under the overlay's OWN
    prompt (`code-review-security`, …), not under the `batch.prompt` finder, so an overlay row's
    `op` is an overlay name. And both batch blocks declare the SAME overlay prompt list (asserted
    below), so which pass produced a given overlay row is not recoverable from `op` at all — only
    the step id carries it.
    """
    rows, _ = gate_run
    steps = {r["step"] for r in rows}
    assert {"round_a", "round_b"} <= steps, f"both batch passes must appear: {sorted(steps)}"

    doc = yaml.safe_load(_GATE.read_text())
    by_id = {s["id"]: s for s in doc["steps"]}
    prompts_a = {c["prompt"] for c in by_id["round_a"]["batch"]["criteria"]}
    prompts_b = {c["prompt"] for c in by_id["round_b"]["batch"]["criteria"]}
    assert prompts_a == prompts_b, (
        "the premise of this test is that the two passes share their overlay prompt space; "
        "if that changed, prompt-name attribution may now be sufficient and this test should be "
        "revisited rather than relaxed"
    )


def test_usage_log_records_the_declared_class_when_the_model_came_from_one(gate_run):
    """Lets a reader tell "opus because frontier" from "opus because cfg.model".

    Covers the BATCH steps too, and covers EVERY row rather than one row per step. Both matter:
    the batch steps bind their token from `batch.model_ladder[0]` through a separate
    `step_identity` call in `_run_batch`, so asserting only the prompt steps would leave that
    binding unguarded; and a batch pass emits one row per overlay, so keying a dict by step id
    would silently assert against whichever row happened to be last.
    """
    rows, _ = gate_run
    expected = {
        "base": "frontier",
        "round_a": "frontier",
        "round_b": "frontier",
        "verify": "standard",
        "coach": "standard",
    }
    seen = {r["step"] for r in rows}
    assert set(expected) <= seen, f"a declaring step produced no usage row: {sorted(seen)}"
    for row in rows:
        want = expected.get(row["step"])
        if want is not None:
            assert row.get("model_class") == want, (
                f"step {row['step']!r} (op {row.get('op')!r}) recorded model_class "
                f"{row.get('model_class')!r}, expected {want!r}"
            )


# ── the declared class reaches model resolution (ACs 5-8, beneath the runner) ────────────────


def test_every_llm_step_resolves_its_declared_class(gate_run):
    """All five LLM-bearing declarations, both shapes: step-level `model:` (`base`, `verify`,
    `coach`) and `batch.model_ladder` (`round_a`, `round_b`)."""
    _, pairs = gate_run
    by_step = _models_by_step(pairs)
    assert by_step["base"] == {_FRONTIER}
    assert by_step["round_a"] == {_FRONTIER}
    assert by_step["round_b"] == {_FRONTIER}
    assert by_step["verify"] == {_STANDARD}  # 172e's declaration, previously unhonoured
    assert by_step["coach"] == {_STANDARD}


def test_no_step_resolves_to_the_config_default(gate_run):
    """The discriminating assertion: every LLM step here declares a class, so cfg.model reaching
    the resolver means a declaration was dropped."""
    _, pairs = gate_run
    offenders = {step for step, model in pairs if model == _CFG_MODEL}
    assert not offenders, f"steps that fell through to cfg.model: {sorted(offenders)}"


def test_no_model_resolution_happens_outside_a_step(gate_run):
    _, pairs = gate_run
    assert None not in _models_by_step(pairs), "a model was resolved with no owning step"
