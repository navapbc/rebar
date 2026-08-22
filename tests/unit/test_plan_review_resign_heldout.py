"""Held-out re-sign enforcement contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rebar.llm.plan_review import attest, resign


def _payload() -> dict:
    return {
        "verdict": "PASS",
        "ticket_id": "1111-2222-3333-4444",
        "material_fingerprint": "aaaaaaaaaaaaaaaa",
        "coverage": {},
        "reviewed_related_material": [
            {
                "role": "child",
                "canonical_id": "aaaa-bbbb-cccc-dddd",
                "material_fingerprint": "1111111111111111",
            }
        ],
    }


def _generation(monkeypatch: pytest.MonkeyPatch) -> None:
    child_id = "aaaa-bbbb-cccc-dddd"
    snapshot = SimpleNamespace(
        child_ids=(child_id,),
        ticket_states_by_id={
            child_id: {
                "ticket_id": child_id,
                "status": "open",
                "file_impact": [{"path": "child.py"}],
                "file_impact_scope": "paths",
            }
        },
    )
    monkeypatch.setattr(
        "rebar.llm.plan_review.generation.collect",
        lambda *a, **k: SimpleNamespace(
            own_material="a" * 16,
            phase="planning",
            relation_snapshot=snapshot,
        ),
    )


def test_enforced_stale_sidecar_pins_refuse_resign_without_compute_validity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert hasattr(attest, "_read_enforce_plan_material_pins")
    _generation(monkeypatch)
    monkeypatch.setattr(resign.sidecar, "latest_review_result", lambda *a, **k: _payload())
    monkeypatch.setattr(
        attest,
        "current_material_fingerprint",
        lambda ticket_id, **k: {
            "1111-2222-3333-4444": "aaaaaaaaaaaaaaaa",
            "aaaa-bbbb-cccc-dddd": "2222222222222222",
        }[ticket_id],
    )
    monkeypatch.setattr(attest, "_read_enforce_plan_material_pins", lambda root: True)
    monkeypatch.setattr(
        attest,
        "compute_validity",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    monkeypatch.setattr(
        attest,
        "sign_plan_review",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not sign stale pins")),
    )
    result = resign.resign_plan_review("1111-2222-3333-4444", repo_root="/repo")
    assert result["ok"] is result["signed"] is False
    assert result["verdict"] == "stale-pin-drift"


def test_legacy_sidecar_remains_resignable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert callable(getattr(resign.sidecar, "parse_reviewed_related_material", None))
    _generation(monkeypatch)
    payload = _payload()
    payload.pop("reviewed_related_material")
    monkeypatch.setattr(resign.sidecar, "latest_review_result", lambda *a, **k: payload)
    monkeypatch.setattr(attest, "current_material_fingerprint", lambda *a, **k: "a" * 16)
    monkeypatch.setattr(attest, "_read_enforce_plan_material_pins", lambda root: True)
    monkeypatch.setattr(
        attest, "sign_plan_review", lambda *a, **k: {"key_id": "k", "head_sha": "h"}
    )
    result = resign.resign_plan_review("1111-2222-3333-4444", repo_root="/repo")
    assert result["ok"] is result["signed"] is True


def _sound_generation() -> SimpleNamespace:
    return SimpleNamespace(
        own_material="a" * 16,
        phase="planning",
        relation_snapshot=SimpleNamespace(child_ids=(), ticket_states_by_id={}),
    )


def _raising_collect(reason: str, fail_times: int):
    """generation.collect stub: raise ``reason`` the first ``fail_times`` calls."""
    from rebar.llm.plan_review.relation_snapshot import PlanRelationSnapshotError

    calls = {"n": 0}

    def _collect(*a, **k):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise PlanRelationSnapshotError(reason)
        return _sound_generation()

    return _collect, calls


def _resignable_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _payload()
    payload.pop("reviewed_related_material")
    monkeypatch.setattr(resign.sidecar, "latest_review_result", lambda *a, **k: payload)
    monkeypatch.setattr(attest, "current_material_fingerprint", lambda *a, **k: "a" * 16)
    monkeypatch.setattr(attest, "_read_enforce_plan_material_pins", lambda root: False)
    monkeypatch.setattr(
        attest, "sign_plan_review", lambda *a, **k: {"key_id": "k", "head_sha": "h"}
    )


def test_transient_store_read_failure_is_retried_then_resigns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A peer session's in-flight staged index (transient store-read-failure) during the
    baseline collect must be retried, not returned as the terminal one-shot refusal
    (bug 90c1-c112, parity sibling of ec1e / staged-index race)."""
    _resignable_payload(monkeypatch)
    collect, calls = _raising_collect("store-read-failure", fail_times=1)
    monkeypatch.setattr("rebar.llm.plan_review.generation.collect", collect)
    result = resign.resign_plan_review("1111-2222-3333-4444", repo_root="/repo")
    assert result["ok"] is result["signed"] is True
    assert calls["n"] == 2


def test_persistent_store_read_failure_is_a_retry_after_repair_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store-read-failure that never clears exhausts the bounded retry budget and
    surfaces as a structured refusal that advises retrying — not a one-shot abort."""
    from rebar.llm.plan_review import generation

    _resignable_payload(monkeypatch)
    monkeypatch.setattr(
        attest,
        "sign_plan_review",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not sign")),
    )
    collect, calls = _raising_collect("store-read-failure", fail_times=99)
    monkeypatch.setattr("rebar.llm.plan_review.generation.collect", collect)
    result = resign.resign_plan_review("1111-2222-3333-4444", repo_root="/repo")
    assert result["ok"] is result["signed"] is False
    assert "store-read-failure" in result["reason"]
    assert "sign-review" in result["reason"]
    assert calls["n"] == generation.MAX_GENERATION_ATTEMPTS


def test_deterministic_collect_failure_stays_terminal_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the transient store-read-failure is retried; a deterministic snapshot reason
    (e.g. a malformed reference) must still refuse on the FIRST attempt."""
    _resignable_payload(monkeypatch)
    collect, calls = _raising_collect("malformed-reference", fail_times=99)
    monkeypatch.setattr("rebar.llm.plan_review.generation.collect", collect)
    result = resign.resign_plan_review("1111-2222-3333-4444", repo_root="/repo")
    assert result["ok"] is result["signed"] is False
    assert "could not be collected" in result["reason"]
    assert calls["n"] == 1
