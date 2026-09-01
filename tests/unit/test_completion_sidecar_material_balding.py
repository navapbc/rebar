from __future__ import annotations


def _basis(receipt_digest: str) -> dict:
    return {
        "schema": "completion_read_basis_v1",
        "run_id": "run-balding",
        "code_oid": "a" * 40,
        "tickets_oid": "b" * 40,
        "receipt_digest": receipt_digest,
        "receipt": {
            "schema": "ticket_read_receipt_v1",
            "view_schema_version": 1,
            "reducer_schema_version": 1,
            "tickets_oid": "b" * 40,
            "exact": {"ticket-1": "comment-aware-state-digest"},
            "fields": {},
            "resolutions": {"ticket-1": {"value": "ticket-1"}},
            "negative": [],
            "direct_children": {},
            "descendants": {},
            "inbound": {},
            "outbound": {},
            "reachability": {},
        },
    }


def _fail_verdict(receipt_digest: str = "r1") -> dict:
    return {
        "verdict": "FAIL",
        "ticket_id": "ticket-1",
        "findings": [
            {
                "criterion": "AC1",
                "detail": "not met",
                "severity": "high",
                "dimension": "completion",
            }
        ],
        "material_fingerprint": "plan-material",
        "completion_read_basis": _basis(receipt_digest),
        "completion_prefetch_manifest": [
            {"path": "src/rebar/llm/completion_sidecar.py", "mode": "full"}
        ],
    }


def _version() -> dict:
    return {
        "prompt_id": "completion-verifier",
        "prompt_content_sha256": "c" * 64,
        "formula_version": "0.13.1 (abc123)",
    }


def test_fail_emit_carries_pinned_material_fingerprint_and_verifier_version(monkeypatch):
    from rebar import config
    from rebar.llm import completion_sidecar

    captured: dict[str, dict] = {}

    def capture(ticket_id, payload, tracker, repo_root):
        captured["ticket_id"] = ticket_id
        captured["payload"] = payload

    monkeypatch.setattr(config, "tracker_dir", lambda repo_root=None: ".tickets-tracker")
    monkeypatch.setattr(completion_sidecar, "_append_sidecar_retrying", capture)
    monkeypatch.setattr(
        completion_sidecar, "verifier_version", lambda repo_root=None: _version(), raising=False
    )

    assert completion_sidecar.emit(_fail_verdict(), repo_root=".")

    payload = captured["payload"]
    assert captured["ticket_id"] == "ticket-1"
    assert payload["schema"] == completion_sidecar.SCHEMA
    assert payload["material_basis"] == "pinned_completion_inputs"
    assert payload["material_fingerprint"]
    assert payload["material_fingerprint"] != "plan-material"
    assert payload["verifier_version"] == _version()
    assert payload["completion_prefetch_manifest"] == [
        {"path": "src/rebar/llm/completion_sidecar.py", "mode": "full"}
    ]

    changed = completion_sidecar.build_payload(
        _fail_verdict("different-receipt"), verifier_version=_version()
    )
    assert changed["material_fingerprint"] != payload["material_fingerprint"]


def test_pass_payload_direct_builder_carries_material_and_version(monkeypatch):
    from rebar.llm import completion_sidecar

    monkeypatch.setattr(completion_sidecar, "verifier_version", lambda repo_root=None: _version())
    verdict = {
        **_fail_verdict(),
        "verdict": "PASS",
        "findings": [],
        "criteria": [{"criterion": "AC1", "met": True}],
        "certifiable": True,
    }

    payload = completion_sidecar.build_payload(verdict, material="explicit-plan-material")

    assert payload["schema"] == completion_sidecar.SCHEMA_PASS
    assert payload["material_basis"] == "pinned_completion_inputs"
    assert payload["material_fingerprint"]
    assert payload["material_fingerprint"] != "explicit-plan-material"
    assert payload["verifier_version"] == _version()


def test_unpinned_fail_payload_marks_material_basis(monkeypatch):
    from rebar.llm import completion_sidecar

    monkeypatch.setattr(completion_sidecar, "verifier_version", lambda repo_root=None: _version())
    verdict = _fail_verdict()
    verdict.pop("completion_read_basis")

    payload = completion_sidecar.build_payload(verdict)

    assert payload["material_basis"] == "unpinned_completion_inputs"
    assert payload["material_fingerprint"]


def test_completion_reconcile_carries_prefetch_manifest_to_sidecar_material(monkeypatch):
    from types import SimpleNamespace

    from rebar.llm.workflow.executor import StepContext
    from rebar.llm.workflow.gate_ops import completion_reconcile

    monkeypatch.setattr(
        "rebar.llm.config.resolve_gate_config",
        lambda repo_root=None: SimpleNamespace(repo_path="."),
    )
    monkeypatch.setattr("rebar.llm.findings.resolve_citations", lambda result, repo_path: None)

    out = completion_reconcile(
        StepContext(
            run_id="run-1",
            step_id="reconcile",
            kind="uses",
            step={},
            inputs={
                "ticket_id": "ticket-1",
                "raw_verdict": "PASS",
                "raw_findings": [],
                "raw_criteria": [{"criterion": "AC1", "met": True}],
                "completion_prefetch_manifest": [{"path": "src/a.py", "mode": "full"}],
                "runner": "fake",
            },
            workflow={},
            repo_root=".",
        )
    )

    assert out["completion_prefetch_manifest"] == [{"path": "src/a.py", "mode": "full"}]


def test_completion_gate_error_emit_enriches_only_completion_records(monkeypatch):
    from rebar.llm import completion_sidecar, gate_error_sidecar

    captured = []

    monkeypatch.setattr(
        completion_sidecar,
        "error_material",
        lambda ticket_id, repo_root=None: ("err-material", "error_unpinned"),
    )
    monkeypatch.setattr(completion_sidecar, "verifier_version", lambda repo_root=None: _version())
    monkeypatch.setattr("rebar.config.tracker_dir", lambda repo_root=None: ".")
    monkeypatch.setattr(
        "rebar._commands._seam.append_event",
        lambda ticket_id, event_type, payload, tracker, repo_root=None: captured.append(payload),
    )

    assert gate_error_sidecar.emit_gate_error("ticket-1", "completion", cause="outage")
    assert gate_error_sidecar.emit_gate_error("ticket-1", "plan_review", cause="outage")

    completion = captured[0]
    plan = captured[1]
    assert completion["material_fingerprint"] == "err-material"
    assert completion["material_basis"] == "error_unpinned"
    assert completion["verifier_version"] == _version()
    assert "material_fingerprint" not in plan
    assert "verifier_version" not in plan


def test_legacy_payloads_without_material_still_round_trip(monkeypatch, tmp_path):
    import json
    import os
    from pathlib import Path

    from rebar import config
    from rebar.llm import completion_sidecar

    tracker = tmp_path / "tracker"
    ticket_dir = tracker / "ticket-1"
    ticket_dir.mkdir(parents=True)
    event = {
        "event_type": completion_sidecar.EVENT_TYPE,
        "data": {
            "schema": completion_sidecar.SCHEMA,
            "verdict": "FAIL",
            "ticket_id": "ticket-1",
            "findings": [],
        },
    }
    (ticket_dir / "0000000000000000001-COMPLETION_VERDICT.json").write_text(
        json.dumps(event), encoding="utf-8"
    )
    pass_event = {
        "event_type": completion_sidecar.EVENT_TYPE,
        "data": {
            "schema": completion_sidecar.SCHEMA_PASS,
            "verdict": "PASS",
            "ticket_id": "ticket-1",
            "criteria": [],
            "findings": [],
        },
    }
    (ticket_dir / "0000000000000000002-COMPLETION_VERDICT.json").write_text(
        json.dumps(pass_event), encoding="utf-8"
    )

    monkeypatch.setattr(config, "tracker_dir", lambda repo_root=None: tracker)
    monkeypatch.setattr(
        "rebar._engine_support.resolver.resolve_ticket_dir_name",
        lambda ticket_id, tracker: os.fspath(Path(ticket_id)),
    )

    assert completion_sidecar.latest_fail_verdict("ticket-1", repo_root=".") == event["data"]
    assert completion_sidecar.latest_pass_record("ticket-1", repo_root=".") == pass_event["data"]
