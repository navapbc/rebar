"""Happy-path slice shared with the held-out implementation agent."""

from __future__ import annotations

from rebar.llm.plan_review import attest, orchestrator, sidecar
from rebar.reducer._processors import process_status
from rebar.reducer._state import make_initial_state


def test_execution_phase_flows_through_reducer_manifest_sidecar_and_pass3(monkeypatch) -> None:
    state = make_initial_state()
    event = {
        "uuid": "a",
        "timestamp": 1,
        "env_id": "env",
        "data": {"current_status": "open", "status": "in_progress"},
    }
    process_status(state, event, event["data"], "event")
    assert state["plan_review_phase"] == "execution"

    verdict = {
        "verdict": "PASS",
        "ticket_id": "1111-2222-3333-4444",
        "coverage": {"counts": {}},
    }
    manifest = attest.build_manifest(
        verdict,
        material="1111111111111111",
        review_phase=state["plan_review_phase"],
        priority_floor=0.8,
    )
    assert attest.manifest_review_phase(manifest) == "execution"
    assert attest.manifest_priority_floor(manifest) == 0.8

    payload = sidecar.build_payload(
        verdict,
        material="1111111111111111",
        review_phase=state["plan_review_phase"],
        priority_floor=0.8,
    )
    assert sidecar.parse_review_phase_metadata(payload) == {
        "phase": "execution",
        "priority_floor": 0.8,
    }

    monkeypatch.setattr(
        orchestrator._criteria,
        "threshold_for",
        lambda criteria, descriptors, *, gate: (0.65, True),
    )
    captured = {}

    def fake_kernel(findings, verifs, *, threshold_for, impact_fn):
        captured["threshold"] = threshold_for("T1")
        return findings

    monkeypatch.setattr(orchestrator.review_kernel, "pass3_over_findings", fake_kernel)
    orchestrator.pass3_over_findings([], {}, execution_review=True)
    assert captured["threshold"] == (0.8, True)
