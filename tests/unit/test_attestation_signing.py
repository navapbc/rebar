"""Signing-call-site tests for the attestations epic (story 2c2d, epic dark-acme-lumen).

Pins that the two signers stamp the unsigned ``data["kind"]`` routing hint, that the hint
does not enter the signed payload (so old signatures still verify and there's no
PAYLOAD_VERSION bump), and that the completion manifest records the material fingerprint so
completion validity-on-read can detect post-signing material edits.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import signing
from rebar._commands.transition_close import _verdict_manifest


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SIGNING_KEY", "test-signing-key-2c2d")
    rebar.init_repo(repo_root=str(repo))
    return repo


def test_sign_manifest_kind_routes_to_attestations_and_verifies(store: Path) -> None:
    tid = rebar.create_ticket("task", "kind passthrough", repo_root=str(store))
    rec = signing.sign_manifest(
        tid, ["plan-review: PASS", "material: m"], kind="plan-review", repo_root=str(store)
    )
    assert rec["kind"] == "plan-review"
    # Still HMAC-certified — the unsigned hint is not part of the signed payload.
    assert signing.verify_signature(tid, repo_root=str(store))["verdict"] == "certified"
    # Reduced into the kind-keyed map under the (manifest-authoritative) kind.
    state = rebar.show_ticket(tid, repo_root=str(store))
    assert list(state["attestations"]) == ["plan-review"]


def test_sign_manifest_without_kind_omits_field(store: Path) -> None:
    # Back-compat: a caller that does not pass kind signs exactly as before (no kind key).
    tid = rebar.create_ticket("task", "no kind", repo_root=str(store))
    rec = signing.sign_manifest(tid, ["plan-review: PASS"], repo_root=str(store))
    assert "kind" not in rec
    # The reducer still derives the kind from the signed manifest[0].
    state = rebar.show_ticket(tid, repo_root=str(store))
    assert list(state["attestations"]) == ["plan-review"]


def test_verdict_manifest_records_material_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    # The completion manifest must carry the material fingerprint (symmetric with plan-review)
    # so completion validity-on-read can detect a post-signing material edit.
    monkeypatch.setattr(
        "rebar.llm.plan_review.attest.current_material_fingerprint",
        lambda ticket_id, repo_root=None: "fp-abc123",
    )
    manifest = _verdict_manifest({"model": "m", "runner": "r"}, "tid-1", repo_root="/x")
    assert manifest[0] == "completion-verifier: PASS"
    assert "material: fp-abc123" in manifest


def test_verdict_manifest_omits_material_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "rebar.llm.plan_review.attest.current_material_fingerprint",
        lambda ticket_id, repo_root=None: None,
    )
    manifest = _verdict_manifest({"model": "m", "runner": "r"}, "tid-1", repo_root="/x")
    assert manifest[0] == "completion-verifier: PASS"
    assert not any(line.startswith("material:") for line in manifest)
