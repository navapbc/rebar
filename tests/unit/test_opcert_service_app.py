"""HTTP-level tests for the trusted op-cert gate service app (story ee0b).

Drives the FastAPI app end-to-end with a TestClient: the async job round-trip (202 {job_id} →
GET reaches a terminal status via the background worker), the origin guard (403 before enqueue),
and the "client fields are ignored" contract (only ticket_id/kind reach the worker). The gate
worker (`jobs.run_job`) is faked so no fetch / signing / LLM runs — this tier tests the HTTP +
queue + worker PLUMBING; the worker logic is covered by test_opcert_service.py.

Skipped when the `reviewbot` extra (fastapi + a TestClient HTTP backend) is absent — mirroring the
review-bot app's offline posture (its endpoints are likewise not exercised without the extra).
"""

from __future__ import annotations

import time

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")  # starlette TestClient's HTTP backend
from fastapi.testclient import TestClient  # noqa: E402

from rebar.opcert_service import jobs  # noqa: E402
from rebar.opcert_service.config import OpcertServiceConfig  # noqa: E402

pytestmark = pytest.mark.unit

GUARD = "shared-secret-value"


def _fake_completed(**kwargs):
    fields = jobs.new_record("", kwargs["ticket_id"], kwargs["kind"])
    fields.pop("job_id")
    fields.update(
        status="completed",
        verdict="PASS",
        envelope="ENCODED-DSSE",
        material_fingerprint="server-derived-fp",
        merged_log_commit="a" * 40,
    )
    return fields


@pytest.fixture
def client(monkeypatch):
    from rebar.opcert_service import app as app_module

    app_module.app.state.config = OpcertServiceConfig(guard=GUARD, job_timeout_seconds=30.0)
    with TestClient(app_module.app) as c:
        yield c


def _poll(client, job_id, *, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rec = client.get(f"/opcert/jobs/{job_id}").json()
        if rec["status"] in ("completed", "error"):
            return rec
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not reach a terminal status")


def test_round_trip_ack_202_then_terminal(client, monkeypatch):
    monkeypatch.setattr(jobs, "run_job", _fake_completed)
    resp = client.post(
        "/opcert/jobs",
        json={"ticket_id": "ee0b-c5e2-3454-42f1", "kind": "completion-verifier"},
        headers={"X-Opcert-Guard": GUARD},
    )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert job_id

    rec = _poll(client, job_id)
    assert rec["status"] == "completed"
    assert rec["kind"] == "completion-verifier"
    assert rec["verdict"] == "PASS"
    assert rec["envelope"] == "ENCODED-DSSE"
    assert rec["material_fingerprint"] == "server-derived-fp"
    assert rec["error"] is None


def test_client_supplied_fields_are_ignored(client, monkeypatch):
    seen = {}

    def spy(**kwargs):
        seen.update(kwargs)
        return _fake_completed(**kwargs)

    monkeypatch.setattr(jobs, "run_job", spy)
    resp = client.post(
        "/opcert/jobs",
        json={
            "ticket_id": "ee0b-c5e2-3454-42f1",
            "kind": "completion-verifier",
            # All of these are attacker-supplied and MUST be ignored — the server derives its own.
            "material_fingerprint": "attacker-forged",
            "commit": "b" * 40,
            "env_id": "attacker-env",
        },
        headers={"X-Opcert-Guard": GUARD},
    )
    assert resp.status_code == 202
    _poll(client, resp.json()["job_id"])
    # run_job only ever receives ticket_id + kind; the doctored fields never reach it.
    assert seen["ticket_id"] == "ee0b-c5e2-3454-42f1"
    assert seen["kind"] == "completion-verifier"
    assert "material_fingerprint" not in seen
    assert "commit" not in seen
    assert "env_id" not in seen


def test_guard_missing_or_mismatch_is_403_before_enqueue(client, monkeypatch):
    called = {"n": 0}

    def counting(**kwargs):
        called["n"] += 1
        return _fake_completed(**kwargs)

    monkeypatch.setattr(jobs, "run_job", counting)

    # Missing header → 403.
    r1 = client.post("/opcert/jobs", json={"ticket_id": "t", "kind": "plan-review"})
    assert r1.status_code == 403
    # Wrong header → 403.
    r2 = client.post(
        "/opcert/jobs",
        json={"ticket_id": "t", "kind": "plan-review"},
        headers={"X-Opcert-Guard": "wrong"},
    )
    assert r2.status_code == 403
    # Nothing was enqueued / run.
    time.sleep(0.1)
    assert called["n"] == 0


def test_matching_guard_is_accepted(client, monkeypatch):
    monkeypatch.setattr(jobs, "run_job", _fake_completed)
    resp = client.post(
        "/opcert/jobs",
        json={"ticket_id": "t", "kind": "plan-review"},
        headers={"X-Opcert-Guard": GUARD},
    )
    assert resp.status_code == 202


def test_invalid_kind_is_400(client):
    resp = client.post(
        "/opcert/jobs",
        json={"ticket_id": "t", "kind": "not-a-kind"},
        headers={"X-Opcert-Guard": GUARD},
    )
    assert resp.status_code == 400


def test_unknown_job_is_404(client):
    assert client.get("/opcert/jobs/deadbeef").status_code == 404
