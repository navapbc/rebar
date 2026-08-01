"""343b gap 5: `provider_provenance` must reach a PRODUCTION verdict payload.

`tests/unit/test_verdict_provenance.py` pins the record's SHAPE and the sidecar's persistence of
it, and all of it passed while production sidecars recorded nothing. MEASURED on a real
`rebar review-plan` run: the written REVIEW_RESULT sidecar had `model: claude-sonnet-4-6` and
`provider_provenance: ABSENT`.

WHY THOSE TESTS MISSED IT — the failure mode this file exists to prevent. Every one of them hands
`build_payload` a verdict dict that ALREADY contains `provider_provenance`. That proves the
persist step and nothing else. The production question — does the verdict that REACHES
`build_payload` ever carry the key — was never asked. A constructed input validated the half of
the contract that was already working.

So the rule here: assert on the verdict produced by the REAL assembly path, and on the REAL
production wiring. Two distinct things must both hold, because the bug lived in the gap between
them:

  1. `finalize_verdict` must PROPAGATE a provenance record it is given, and
  2. the gate YAML must actually PASS one — the runner already stamps the record onto the verify
     step's outputs, and nothing was carrying it forward.

A test for (1) alone would have passed against the broken tree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from rebar.llm.capabilities import ModelCapabilities, provenance_for

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATES = _REPO_ROOT / "src" / "rebar" / "llm" / "workflow" / "gates"
_PLAN_REVIEW_YAML = _GATES / "plan-review.yaml"
_COMPLETION_YAML = _GATES / "completion-verification.yaml"

CAPS = ModelCapabilities(
    native_structured_output=True,
    prompt_cache_style="anthropic",
    supports_thinking=False,
    supports_temperature=True,
)

RECORD = provenance_for(
    provider="bedrock",
    model="bedrock:us.anthropic.claude-sonnet-4-6",
    base_url=None,
    caps=CAPS,
)


def _steps(doc: Any) -> list[dict[str, Any]]:
    """Every step mapping in a gate document, flattened through `branch`/`else` arms — the coach
    step lives inside a branch, so a top-level scan would miss it entirely."""
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "id" in node and ("uses" in node or "prompt" in node):
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)
    return found


def _steps_using(gate_yaml: Path, op: str) -> list[dict[str, Any]]:
    return [s for s in _steps(yaml.safe_load(gate_yaml.read_text())) if s.get("uses") == op]


# ══ HAPPY PATH ════════════════════════════════════════════════════════════════════════


def test_finalize_verdict_propagates_a_provenance_record() -> None:
    """(1) The assembly function must carry a record it is handed onto the verdict it returns."""
    from rebar.llm.plan_review import orchestrator
    from rebar.llm.plan_review.det_floor import PlanContext

    verdict = orchestrator.finalize_verdict(
        PlanContext(ticket_id="abcd-0000-0000-0001", ticket_type="task", title="", description=""),
        {"blocking": [], "surfaced": [], "overflow": [], "indeterminate": [], "dropped": []},
        coaching=[],
        coverage={"llm_ran": True},
        runner_name="fake",
        model="bedrock:us.anthropic.claude-sonnet-4-6",
        provider_provenance=RECORD,
    )
    assert verdict["provider_provenance"] == RECORD


def test_plan_review_yaml_wires_provenance_into_every_coach_arm() -> None:
    """(2) THE MISSING WIRE, and the assertion that would have caught this bug.

    `RunnerAgentStep.run` returns the runner result verbatim as step outputs, and the runner
    already stamps `provider_provenance` onto it — so `steps.verify.outputs.provider_provenance`
    existed all along and simply was not passed on. The coach step is duplicated across a
    branch's two arms, so BOTH must wire it; wiring one is how half a fix ships.
    """
    arms = _steps_using(_PLAN_REVIEW_YAML, "plan_review_coach")
    assert len(arms) >= 2, f"expected both coach arms, found {len(arms)}"
    for arm in arms:
        wired = (arm.get("with") or {}).get("provider_provenance")
        assert wired, f"coach arm {arm.get('id')!r} does not wire provider_provenance"
        assert "verify" in str(wired) and "provider_provenance" in str(wired), wired


def test_coach_op_puts_provenance_on_the_verdict_it_returns(tmp_path) -> None:
    """The two halves joined: drive the REAL `plan_review_coach` op with the input the YAML now
    wires, and assert the verdict it returns carries the record."""
    from rebar.llm.plan_review.workflow_ops import plan_review_coach
    from rebar.llm.workflow.executor import StepContext

    ctx = StepContext(
        run_id="run-1",
        step_id="coach",
        kind="op",
        step={"uses": "plan_review_coach"},
        inputs={
            "blocking": [],
            "surfaced": [],
            "overflow": [],
            "indeterminate": [],
            "dropped": [],
            "notes": [],
            "canonical_id": "abcd-0000-0000-0001",
            "ticket_type": "task",
            "provider_provenance": RECORD,
        },
        workflow={},
        target_ticket="abcd-0000-0000-0001",
        repo_root=str(tmp_path),
    )
    verdict = plan_review_coach(ctx)
    assert verdict["provider_provenance"] == RECORD


def test_completion_gate_carries_provenance_from_its_verify_step() -> None:
    """The completion gate has the same defect and the same one-wire fix: its `verify` step is an
    agent step, so its outputs already carry the record; `completion_sidecar` reads a key the
    verdict never set."""
    doc = yaml.safe_load(_COMPLETION_YAML.read_text())
    reconcile = [s for s in _steps(doc) if s.get("uses") == "completion_reconcile"]
    assert reconcile, "no completion_reconcile step found"
    for step in reconcile:
        wired = (step.get("with") or {}).get("provider_provenance")
        assert wired, "completion_reconcile does not wire provider_provenance"
        assert "verify" in str(wired), wired


# ══ HELD OUT ══════════════════════════════════════════════════════════════════════════


def test_a_real_coach_verdict_survives_into_the_sidecar_payload(tmp_path) -> None:
    """THE END-TO-END ORACLE — the one test whose absence let this ship.

    It deliberately does NOT hand-build a verdict. It runs the real coach op, takes whatever
    verdict comes out, and feeds THAT to the real `build_payload`. That composition is the
    production path, and it is the only assertion here that fails if either half regresses.
    """
    from rebar.llm.plan_review import sidecar
    from rebar.llm.plan_review.workflow_ops import plan_review_coach
    from rebar.llm.workflow.executor import StepContext

    ctx = StepContext(
        run_id="run-1",
        step_id="coach",
        kind="op",
        step={"uses": "plan_review_coach"},
        inputs={
            "blocking": [],
            "surfaced": [],
            "overflow": [],
            "indeterminate": [],
            "dropped": [],
            "notes": [],
            "canonical_id": "abcd-0000-0000-0001",
            "ticket_type": "task",
            "provider_provenance": RECORD,
        },
        workflow={},
        target_ticket="abcd-0000-0000-0001",
        repo_root=str(tmp_path),
    )
    verdict = plan_review_coach(ctx)
    payload = sidecar.build_payload(verdict, repo_root=str(tmp_path))

    assert payload["provider_provenance"] == RECORD
    # Round-trips as JSON: a signed sidecar is serialized before it is signed.
    assert json.loads(json.dumps(payload))["provider_provenance"]["tier"] == "first_class"


def test_a_verdict_no_model_produced_does_not_invent_provenance() -> None:
    """The honest-value rule for the three sites where NO LLM ran (the DET short-circuit, the
    verify-failure recovery, and the outage degrade).

    Those sites hold `cfg` and already report `model=cfg.model`, so synthesizing a record from it
    is the obvious move — and it is WRONG. `provenance_for` documents that a recomputed record can
    diverge from the one that actually drove the run, and here nothing ran at all. A cfg-derived
    record would make the verdict claim a provider served it when none did, which is the exact
    misattribution this record was introduced to remove. Omit the key instead; the sidecar already
    tolerates absence.
    """
    from rebar.llm.plan_review import orchestrator
    from rebar.llm.plan_review.det_floor import PlanContext

    verdict = orchestrator.finalize_verdict(
        PlanContext(ticket_id="abcd-0000-0000-0001", ticket_type="task", title="", description=""),
        {"blocking": [], "surfaced": [], "overflow": [], "indeterminate": [], "dropped": []},
        coaching=[],
        coverage={"llm_ran": False},
        runner_name="fake",
        model="claude-opus-4-8",
    )
    assert verdict.get("provider_provenance") is None
    # The model string is still recorded — this is about not FABRICATING observed provenance,
    # not about dropping configured intent.
    assert verdict["model"] == "claude-opus-4-8"


def test_a_legacy_verdict_without_the_key_still_builds_a_payload(tmp_path) -> None:
    """Back-compat: the key is ADDITIVE. A verdict predating it must still produce a payload, with
    provenance absent rather than an exception — signed sidecars from before this change must
    remain readable."""
    from rebar.llm.plan_review import orchestrator
    from rebar.llm.plan_review.det_floor import PlanContext

    verdict = orchestrator.finalize_verdict(
        PlanContext(ticket_id="abcd-0000-0000-0001", ticket_type="task", title="", description=""),
        {"blocking": [], "surfaced": [], "overflow": [], "indeterminate": [], "dropped": []},
        coaching=[],
        coverage={"llm_ran": True},
        runner_name="fake",
        model="claude-opus-4-8",
    )
    payload = sidecar_build(verdict, tmp_path)
    assert payload.get("provider_provenance") is None


def sidecar_build(verdict: dict[str, Any], tmp_path) -> dict[str, Any]:
    from rebar.llm.plan_review import sidecar

    return sidecar.build_payload(verdict, repo_root=str(tmp_path))


@pytest.mark.parametrize("gate_yaml", [_PLAN_REVIEW_YAML, _COMPLETION_YAML])
def test_the_wire_reads_the_step_that_actually_holds_the_record(gate_yaml: Path) -> None:
    """Guard against wiring the record from the wrong step. Only an AGENT step's outputs are the
    runner result verbatim; a scripted op's outputs are whatever that op chose to return, so
    sourcing provenance from one would silently wire `None` forever."""
    doc = yaml.safe_load(gate_yaml.read_text())
    by_id = {s.get("id"): s for s in _steps(doc)}
    for step in _steps(doc):
        wired = (step.get("with") or {}).get("provider_provenance")
        if not wired:
            continue
        src = str(wired).split("steps.", 1)[-1].split(".", 1)[0]
        assert src in by_id, f"{step.get('id')} wires provenance from unknown step {src!r}"
        assert "prompt" in by_id[src], (
            f"{step.get('id')} sources provenance from {src!r}, which is not an agent step; "
            "only an agent step's outputs carry the runner's record"
        )
