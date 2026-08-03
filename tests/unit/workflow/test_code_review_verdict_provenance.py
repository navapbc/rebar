"""Task e951: a code-review verdict must record the provenance of the provider that produced it.

`code_review/sidecar.py` has read `verdict.get("provider_provenance")` since 343b, but the
code-review verdict never set the key — so every signed code-review sidecar recorded provenance as
ABSENT while reporting a bare `model` string taken from cfg. 343b fixed the plan-review and
completion gates and deliberately left code review out.

WHY THIS FILE LOOKS THE WAY IT DOES — 343b's gap 5. All ten of its unit tests passed against a tree
where the field never reached a production payload, because every one of them handed `build_payload`
a verdict dict that ALREADY contained `provider_provenance`. That is a constructed-input pass: the
oracle proved the PERSIST step and never the PATH. So the primary test here hand-builds nothing. It
runs the REAL `code-review.yaml` through the REAL interpreter with a REAL `PydanticAIRunner` (its
model swapped for an offline `FunctionModel`, so nothing billable is called), takes whatever
terminal verdict the gate produces, and feeds THAT to the real sidecar payload builder. Remove the
YAML wire or the op's assignment and it goes red.

The record is assembled ONCE, inside the runner (`capabilities.provenance_for`), and stamped onto
each per-call result by `findings.finalize_outcome`. Nothing here recomputes it: a second resolution
at the verdict site can diverge from the endpoint/caps that actually served the run, which is
exactly what `provenance_for`'s docstring forbids.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import replace

import pytest

pytest.importorskip("pydantic_ai")

import yaml
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from rebar.llm import gate_context
from rebar.llm.code_review import sidecar as code_review_sidecar
from rebar.llm.config import LLMConfig
from rebar.llm.runner import PydanticAIRunner
from rebar.llm.workflow import executor as _ex
from rebar.llm.workflow import migrate as _migrate
from rebar.llm.workflow import steps as _steps  # noqa: F401 — registers the code_review ops
from rebar.llm.workflow.runs import RunnerAgentStep

pytestmark = pytest.mark.unit

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_GATE = _REPO_ROOT / "src" / "rebar" / "llm" / "workflow" / "gates" / "code-review.yaml"

_DIFF = "diff --git a/src/auth/login.py b/src/auth/login.py\n+++ b/src/auth/login.py\n+x\n"

# One payload that satisfies every output schema in this gate at once (the union of the four
# shapes); extra keys are ignored by each step's own schema. The CONTENT is irrelevant here — only
# the provenance record travelling alongside it is under test.
_PAYLOAD: dict = {"findings": [], "recommend_overlays": [], "verifications": [], "notes": []}


def _offline_model(messages, info: AgentInfo) -> ModelResponse:
    """Answer immediately with the step's structured output — never enter a tool loop."""
    if info.output_tools:
        return ModelResponse(
            parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=_PAYLOAD)]
        )
    return ModelResponse(parts=[TextPart(json.dumps(_PAYLOAD))])


def _gate_steps() -> list[dict]:
    """Every step mapping in the gate document, flattened through nested arms."""
    found: list[dict] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if "id" in node and ("uses" in node or "prompt" in node or "batch" in node):
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(yaml.safe_load(_GATE.read_text()))
    return found


# ══ the wire ══════════════════════════════════════════════════════════════════════════


def test_code_review_yaml_wires_provenance_from_an_agent_step() -> None:
    """The verdict-assembly step must source the record from an AGENT step.

    Only an agent step's outputs are the runner result VERBATIM (`workflow/runs.py`), and the
    runner is the one place that assembles the record. Sourcing it from a scripted op — whose
    outputs are whatever that op chose to return — would validate, wire `None` forever, and look
    exactly like a fix.
    """
    steps = _gate_steps()
    by_id = {s.get("id"): s for s in steps}
    assembly = [s for s in steps if s.get("uses") == "code_review_coach"]
    assert assembly, "no code_review_coach (verdict assembly) step found in code-review.yaml"
    for step in assembly:
        wired = (step.get("with") or {}).get("provider_provenance")
        assert wired, f"step {step.get('id')!r} does not wire provider_provenance"
        assert "provider_provenance" in str(wired), wired
        src = str(wired).split("steps.", 1)[-1].split(".", 1)[0]
        assert src in by_id, f"{step.get('id')} wires provenance from unknown step {src!r}"
        assert "prompt" in by_id[src], (
            f"{step.get('id')} sources provenance from {src!r}, which is not an agent step; "
            "only an agent step's outputs carry the runner's record"
        )


# ══ the production path, end to end ═══════════════════════════════════════════════════


@pytest.fixture
def gate_verdict(tmp_path, monkeypatch) -> dict:
    """Run the REAL code-review gate once, offline, and return its terminal verdict.

    Nothing about the verdict is constructed: the interpreter, the gate document, the scripted ops
    and the runner are all the production ones. Only the MODEL is a double, and the provenance
    record is assembled by the runner regardless of that (`runner._pai_model` path), so the record
    under test is the real one.
    """
    monkeypatch.setenv("REBAR_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    doc = _migrate.migrate_to_current(yaml.safe_load(_GATE.read_text()))
    cfg = replace(
        LLMConfig.from_env(),
        runner="pydantic_ai",
        repo_path=".",
        model="anthropic:claude-opus-4-8",
        api_key=None,
    )
    runner = PydanticAIRunner(cfg, model_override=FunctionModel(_offline_model))
    with gate_context.gate_session(), gate_context.use_code_root("."):
        res = _ex.run_workflow(
            doc,
            {"base": "HEAD~1", "head": "HEAD", "diff_text": _DIFF, "changed_files": []},
            scripted_registry=dict(_ex.STEP_REGISTRY),
            agent_runner=RunnerAgentStep(runner=runner, config=cfg),
            batch_runner=_batch_runner(),
        )
    assert res.status == "succeeded", f"the offline gate run failed: {res.error}"
    verdict = res.terminal_output
    assert isinstance(verdict, dict) and "verdict" in verdict, verdict
    return verdict


def _batch_runner():
    from rebar.llm.code_review.batch_runner import CodeReviewBatchRunner

    return CodeReviewBatchRunner(context="## Diff\n(fake)")


def test_a_real_gate_verdict_carries_provenance_into_the_sidecar_payload(gate_verdict) -> None:
    """THE ORACLE — the composition 343b's tests never made.

    A verdict produced by the real assembly path, fed to the real sidecar payload builder. It is
    the only assertion in this file that fails if EITHER half regresses (the YAML wire or
    `code_review_coach`'s assignment), and it cannot be satisfied by a hand-built input because it
    never sees one.
    """
    record = gate_verdict.get("provider_provenance")
    assert record, (
        "the gate's terminal verdict carries no provider_provenance — the record the runner "
        "stamped on the Pass-2 verify step is not reaching the verdict"
    )
    assert record.get("provider"), record
    assert record.get("tier"), record

    payload = code_review_sidecar.build_payload(gate_verdict, target_ticket="abcd-0000-0000-0001")
    assert payload["provider_provenance"] == record
    # A sidecar is serialized before it is signed, so the record must survive a JSON round-trip.
    assert json.loads(json.dumps(payload))["provider_provenance"]["provider"] == record["provider"]


def test_the_real_verdict_reports_more_than_a_bare_model_string(gate_verdict) -> None:
    """The defect in one sentence: `model` alone cannot say which provider or endpoint served the
    run, so a review produced through Bedrock or an OpenAI-compatible gateway was indistinguishable
    from a first-class Anthropic one."""
    record = gate_verdict["provider_provenance"]
    assert set(record) >= {"provider", "model", "tier"}, record


# ══ absence stays absence ═════════════════════════════════════════════════════════════


def test_the_degraded_verdict_omits_provenance_rather_than_deriving_one() -> None:
    """The outage path ran no model, so it must OMIT the key.

    That site can reach `cfg`, which makes synthesizing a record from `cfg.model` the obvious move
    and a wrong one: the verdict would claim a provider served it when none did — the exact
    misattribution the record exists to remove. The sidecar tolerates absence.
    """
    from rebar.llm.workflow.gate_dispatch import _degraded_code_review_verdict

    verdict = _degraded_code_review_verdict(error=RuntimeError("outage"), runner_name="pydantic_ai")
    assert "provider_provenance" not in verdict
    payload = code_review_sidecar.build_payload(verdict, target_ticket="abcd-0000-0000-0001")
    assert payload["provider_provenance"] is None


def _finalize(verdict: dict, *, verify_outputs: dict, tmp_path) -> dict:
    """Drive the real `finalize_code_review_verdict` with a recorder holding one succeeded Pass-2
    `verify` agent step. No target ticket / session / change id, so the emit + novelty floor stay
    inert and the only thing under observation is the provenance stamp."""
    from types import SimpleNamespace

    from rebar.llm.code_review import finalize as _fin

    rec = SimpleNamespace(
        steps=[
            {
                "step_id": "verify",
                "kind": "agent",
                "status": "succeeded",
                "duration_ms": 1.0,
                "outputs": verify_outputs,
            }
        ]
    )
    prep = SimpleNamespace(rec=rec, dc=SimpleNamespace(changed_files=[], diff_text=""))
    request = SimpleNamespace(
        repo_root=str(tmp_path),
        session_id=None,
        change_id="",
        target_ticket=None,
        head="HEAD",
    )
    return _fin.finalize_code_review_verdict(
        verdict,
        request=request,
        prep=prep,
        cfg=SimpleNamespace(model="anthropic:claude-opus-4-8"),
        runner_sel=SimpleNamespace(name="pydantic_ai"),
        total_ms=1.0,
    )


def test_finalize_recovers_the_runners_record_for_an_unwired_verdict(tmp_path) -> None:
    """`finalize` stamps cfg-derived `model`; the OBSERVED record has to travel with it.

    A verdict that arrives without the wire (a gate document that does not carry it) still has the
    runner's record sitting on the recorder's `verify` step. Read back from there it is the SAME
    record the wire delivers — not a fresh resolution, which `provenance_for` forbids.
    """
    record = {"provider": "bedrock", "model": "bedrock:us.anthropic.claude-sonnet-4-6", "tier": "x"}
    verdict = _finalize(
        {"verdict": "PASS", "blocking": [], "advisory": [], "coaching": []},
        verify_outputs={"provider_provenance": record},
        tmp_path=tmp_path,
    )
    assert verdict["provider_provenance"] == record
    assert verdict["model"] == "anthropic:claude-opus-4-8"  # configured intent, still recorded


def test_finalize_invents_nothing_when_no_provider_record_exists(tmp_path) -> None:
    """No record anywhere ⇒ the key stays ABSENT. `finalize` holds `cfg`, so deriving one from
    `cfg.model` is a one-liner away and would make the verdict claim a provider served it."""
    verdict = _finalize(
        {"verdict": "PASS", "blocking": [], "advisory": [], "coaching": []},
        verify_outputs={"provider_provenance": None},
        tmp_path=tmp_path,
    )
    assert "provider_provenance" not in verdict


def test_the_assembly_op_omits_the_key_when_no_provider_resolved(tmp_path) -> None:
    """`_dispatch` defaults an agent step's `provider_provenance` output to None when no provider
    resolved, so the wire delivers None on those runs. The op must then leave the key ABSENT rather
    than recording a null that reads as "provenance was captured"."""
    from rebar.llm.code_review.workflow_ops import code_review_coach
    from rebar.llm.workflow.executor import StepContext

    ctx = StepContext(
        run_id="run-1",
        step_id="verdict",
        kind="op",
        step={"uses": "code_review_coach"},
        inputs={
            "blocking": [],
            "surfaced": [],
            "dropped": [],
            "indeterminate": [],
            "notes": [],
            "provider_provenance": None,
        },
        workflow={},
        target_ticket=None,
        repo_root=str(tmp_path),
    )
    verdict = code_review_coach(ctx)
    assert "provider_provenance" not in verdict
