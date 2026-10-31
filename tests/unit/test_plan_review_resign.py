"""Pin wiring across every plan-review signature-producing path.

Held-out oracles merged from test_plan_review_resign_heldout.py: Held-out re-sign enforcement
contracts.
"""

from __future__ import annotations

import contextlib
import importlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import rebar
from rebar import signing
from rebar.llm.plan_review import attest, resign


def _api():
    try:
        module = importlib.import_module("rebar.llm.plan_review.relation_snapshot")
    except ModuleNotFoundError:
        pytest.fail("plan relation snapshot API is absent")
    pins = (
        module.PlanMaterialPin("child", "aaaa-bbbb-cccc-dddd", "0123456789abcdef"),
        module.PlanMaterialPin("prerequisite", "eeee-ffff-aaaa-bbbb", "fedcba9876543210"),
    )
    return module.PlanRelationSnapshot, pins


def _snapshot(ticket_id: str):
    PlanRelationSnapshot, pins = _api()
    return PlanRelationSnapshot(
        subject_state={"ticket_id": ticket_id},
        ticket_states_by_id={
            ticket_id: {"ticket_id": ticket_id},
            pins[0].canonical_id: {
                "ticket_id": pins[0].canonical_id,
                "status": "open",
                "file_impact": [{"path": "child.py"}],
                "file_impact_scope": "paths",
            },
        },
        child_ids=(pins[0].canonical_id,),
        prerequisite_ids=(pins[1].canonical_id,),
        related_material=pins,
        ticket_store_revision="a" * 40,
    )


def _capture_sign(monkeypatch):
    captured: list[str] = []

    def fake_sign(ticket_id, manifest, **kwargs):
        captured[:] = manifest
        return {"key_id": "key", "head_sha": "head"}

    monkeypatch.setattr("rebar.signing.sign_manifest", fake_sign)
    monkeypatch.setattr(attest, "dependency_hashes", lambda *a, **k: {})
    monkeypatch.setattr(attest, "registry_version", lambda *a, **k: "registry")
    monkeypatch.setattr("rebar.llm.plan_review.registry.disabled_builtins", lambda *a, **k: [])
    # Simulate an active attested session: the sign seam's no-null-pin invariant
    # (bug 5128-0856) refuses to sign with no snapshot SHA at all.
    monkeypatch.setattr("rebar.llm.gate_context.current_code_sha", lambda: "c" * 40)
    return captured


def test_ordinary_signing_collects_and_emits_current_pins(monkeypatch) -> None:
    _, pins = _api()
    ticket_id = "1111-2222-3333-4444"
    monkeypatch.setattr(
        "rebar.llm.plan_review.relation_snapshot.collect_plan_relation_snapshot",
        lambda *a, **k: _snapshot(ticket_id),
    )
    captured = _capture_sign(monkeypatch)
    attest.sign_plan_review(
        {
            "verdict": "PASS",
            "ticket_id": ticket_id,
            "model": "m",
            "runner": "r",
            "coverage": {"counts": {}, "llm_ran": True},
        },
        material="1111111111111111",
    )
    assert attest.manifest_pins(captured) == list(pins)


def test_drift_refresh_collects_and_emits_current_pins(monkeypatch) -> None:
    _, pins = _api()
    ticket_id = "1111-2222-3333-4444"
    monkeypatch.setattr(
        "rebar.llm.plan_review.relation_snapshot.collect_plan_relation_snapshot",
        lambda *a, **k: _snapshot(ticket_id),
    )
    captured = _capture_sign(monkeypatch)
    monkeypatch.setattr("rebar.signing.verify_signature", lambda *a, **k: {"key_id": "old"})
    monkeypatch.setattr(attest, "_rehash", lambda *a, **k: {})
    prior = attest.build_manifest(
        {"verdict": "PASS", "ticket_id": ticket_id, "coverage": {"counts": {}}},
        material="1111111111111111",
    )
    attest.refresh_attestation(ticket_id, prior, probe="PASS")
    assert attest.manifest_pins(captured) == list(pins)


def test_resign_routes_through_pin_collecting_sign_path(monkeypatch) -> None:
    _api()
    ticket_id = "1111-2222-3333-4444"
    payload = {
        "verdict": "PASS",
        "ticket_id": ticket_id,
        "material_fingerprint": "1111111111111111",
        "coverage": {},
    }
    monkeypatch.setattr(resign.sidecar, "latest_review_result", lambda *a, **k: payload)
    generation = SimpleNamespace(
        own_material=payload["material_fingerprint"],
        phase="planning",
        relation_snapshot=_snapshot(ticket_id),
    )
    monkeypatch.setattr("rebar.llm.plan_review.generation.collect", lambda *a, **k: generation)

    # Sandbox the attested gate session. resign signs inside gate_source.gate_read_root
    # after gate_source.resolve_gate_handle (bug 5128-0856), which in attested mode (the
    # suite default) materializes the real HEAD code tree AND the ~121k-file tickets branch
    # into the gate cache — 1.1 GB / 125k files whose later recursive deletion costs minutes
    # of unlinkat/rmdir at session cleanup (anatomical-continuous-akitainu). This is a unit
    # test of routing, so stub the seam exactly as the sibling public-resign tests do; the
    # spy below asserts resign still routes signing through it.
    from rebar.llm import gate_source

    gate_handle_calls: list[tuple] = []

    def _fake_resolve(*args, **kwargs):
        gate_handle_calls.append((args, kwargs))
        return object()

    monkeypatch.setattr(gate_source, "resolve_gate_handle", _fake_resolve)
    monkeypatch.setattr(gate_source, "gate_read_root", lambda *a, **k: contextlib.nullcontext())

    seen = {}

    def fake_sign(verdict, **kwargs):
        seen["ticket_id"] = verdict["ticket_id"]
        seen["generation"] = kwargs["initial_generation"]
        return {"key_id": "key", "head_sha": "head"}

    monkeypatch.setattr(attest, "sign_plan_review", fake_sign)
    result = resign.resign_plan_review(ticket_id)
    assert result["ok"] is True
    assert seen == {"ticket_id": ticket_id, "generation": generation}
    assert gate_handle_calls, "resign must route signing through the attested gate seam"


@pytest.mark.parametrize("snapshot_kind", ["missing", "mismatched-child"])
def test_public_resign_refuses_inconsistent_child_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    snapshot_kind: str,
) -> None:
    ticket_id = "1111-2222-3333-4444"
    payload = {
        "verdict": "PASS",
        "ticket_id": ticket_id,
        "material_fingerprint": "1111111111111111",
        "coverage": {},
    }
    monkeypatch.setattr(resign.sidecar, "latest_review_result", lambda *a, **k: payload)
    if snapshot_kind == "missing":
        generation = SimpleNamespace(
            own_material=payload["material_fingerprint"],
            phase="planning",
        )
    else:
        snapshot = SimpleNamespace(
            child_ids=("aaaa-bbbb-cccc-dddd",),
            ticket_states_by_id={
                "aaaa-bbbb-cccc-dddd": {
                    "ticket_id": "eeee-ffff-aaaa-bbbb",
                    "status": "open",
                    "file_impact": [],
                    "file_impact_scope": "none",
                }
            },
        )
        generation = SimpleNamespace(
            own_material=payload["material_fingerprint"],
            phase="planning",
            relation_snapshot=snapshot,
        )
    monkeypatch.setattr(
        "rebar.llm.plan_review.generation.collect",
        lambda *a, **k: generation,
    )

    result = resign.resign_plan_review(ticket_id)

    assert result["ok"] is False
    assert result["signed"] is False
    assert result["verdict"] == "INDETERMINATE"
    assert result["child_impact_state_error"]["event"] == (
        "plan_review_child_impact_snapshot_invalid"
    )


def test_public_resign_preserves_authenticated_none_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "initial"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    ticket_id = rebar.create_ticket("task", "recover none scope", repo_root=str(repo))
    rebar.declare_no_file_impact(
        ticket_id,
        "external operator action only",
        repo_root=str(repo),
    )

    from rebar.llm import gate_source
    from rebar.llm.plan_review import generation

    material = generation.collect(ticket_id, repo_root=str(repo)).own_material
    payload = {
        "verdict": "PASS",
        "ticket_id": ticket_id,
        "ticket_type": "task",
        "material_fingerprint": material,
        "coverage": {},
    }
    monkeypatch.setattr(resign.sidecar, "latest_review_result", lambda *a, **k: payload)
    monkeypatch.setattr(gate_source, "resolve_gate_handle", lambda *a, **k: object())
    monkeypatch.setattr(gate_source, "gate_read_root", lambda *a, **k: contextlib.nullcontext())
    code_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setattr("rebar.llm.gate_context.current_code_sha", lambda: code_head)

    result = resign.resign_plan_review(ticket_id, repo_root=str(repo))
    verified = signing.verify_signature(ticket_id, kind="plan-review", repo_root=str(repo))

    assert result["ok"] is result["signed"] is True
    assert verified["verified"] is True
    assert attest.manifest_file_scope(verified["manifest"]) == "none"


def test_public_resign_preserves_mixed_child_scope_and_validity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "initial"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    (repo / "child.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "child.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add child path"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    parent = rebar.create_ticket("story", "mixed child scope", repo_root=str(repo))
    path_child = rebar.create_ticket("task", "path child", parent=parent, repo_root=str(repo))
    none_child = rebar.create_ticket("task", "none child", parent=parent, repo_root=str(repo))
    rebar.set_file_impact(
        path_child,
        [{"path": "child.py", "reason": "declared child behavior"}],
        repo_root=str(repo),
    )
    rebar.declare_no_file_impact(
        none_child,
        "coordination only",
        repo_root=str(repo),
    )

    from rebar.llm import gate_source
    from rebar.llm.plan_review import generation

    initial_generation = generation.collect(parent, repo_root=str(repo))
    payload = {
        "verdict": "PASS",
        "ticket_id": parent,
        "ticket_type": "story",
        "material_fingerprint": initial_generation.own_material,
        "coverage": {},
    }
    monkeypatch.setattr(resign.sidecar, "latest_review_result", lambda *a, **k: payload)
    monkeypatch.setattr(gate_source, "resolve_gate_handle", lambda *a, **k: object())
    monkeypatch.setattr(gate_source, "gate_read_root", lambda *a, **k: contextlib.nullcontext())
    code_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setattr("rebar.llm.gate_context.current_code_sha", lambda: code_head)

    result = resign.resign_plan_review(parent, repo_root=str(repo))
    verified = signing.verify_signature(parent, kind="plan-review", repo_root=str(repo))
    parent_state = rebar.show_ticket(parent, repo_root=str(repo))

    assert result["ok"] and result["signed"] is True
    assert verified["verified"] is True
    assert set(attest.manifest_deps(verified["manifest"])) == {"child.py"}

    (repo / "unrelated.py").write_text("UNRELATED = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "unrelated.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "unrelated drift"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    unrelated = attest.compute_validity(
        verified,
        parent_state,
        "plan-review",
        repo_root=str(repo),
    )
    assert unrelated["valid"] is True

    (repo / "child.py").write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "child.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "declared path drift"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    declared = attest.compute_validity(
        verified,
        parent_state,
        "plan-review",
        repo_root=str(repo),
    )
    assert declared["valid"] is False
    assert declared["verdict"] == "stale-code"


def test_public_resign_promotes_all_none_container_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "initial"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)

    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    parent = rebar.create_ticket("story", "all-none child scope", repo_root=str(repo))
    for title in ("none child one", "none child two"):
        child = rebar.create_ticket(
            "task",
            title,
            parent=parent,
            repo_root=str(repo),
        )
        rebar.declare_no_file_impact(child, "coordination only", repo_root=str(repo))

    from rebar.llm import gate_source
    from rebar.llm.plan_review import generation

    initial_generation = generation.collect(parent, repo_root=str(repo))
    payload = {
        "verdict": "PASS",
        "ticket_id": parent,
        "ticket_type": "story",
        "material_fingerprint": initial_generation.own_material,
        "coverage": {},
    }
    monkeypatch.setattr(resign.sidecar, "latest_review_result", lambda *a, **k: payload)
    monkeypatch.setattr(gate_source, "resolve_gate_handle", lambda *a, **k: object())
    monkeypatch.setattr(gate_source, "gate_read_root", lambda *a, **k: contextlib.nullcontext())
    code_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setattr("rebar.llm.gate_context.current_code_sha", lambda: code_head)

    result = resign.resign_plan_review(parent, repo_root=str(repo))
    verified = signing.verify_signature(parent, kind="plan-review", repo_root=str(repo))

    assert result["ok"] and result["signed"] is True
    assert verified["verified"] is True
    assert attest.manifest_deps(verified["manifest"]) == {}
    assert attest.manifest_file_scope(verified["manifest"]) == "none"


# ── held-out oracles (merged from test_plan_review_resign_heldout.py) ──


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
