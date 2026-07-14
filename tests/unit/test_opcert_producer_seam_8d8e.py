"""Producer-signing seam repoint (story 8d8e, epic 6d0d — fork A: uniform producer signing).

The signing seam ``signing.sign_manifest(ticket_id, manifest, *, kind=…)`` now MINTS a
``rebar.opcert.v1`` DSSE op-cert with the environment's auto-generated Ed25519 key (expand phase:
write-new envelopes, read-both envelopes + legacy HMAC). These tests pin the happy paths and the
mechanical invariants the ACs enumerate:

  * dependency-gate round-trip (the e4df contract asserted behaviorally),
  * key genesis + permissions + git-ignore + verify-side-never-creates,
  * plan-review PASS → envelope SIGNATURE event that ``verify_signature`` certifies + never-sign
    guards,
  * drift-refresh / resign re-sign as op-certs,
  * the ``rebar sign`` CLI + library seam emit envelopes,
  * a completion-verifier manifest signs as an op-cert through the same seam,
  * expand-phase read-both (legacy HMAC + envelope coexist, kind-keyed),
  * ``REBAR_OPCERT_ENV_ID`` principal override,
  * schema + ``SignResultOut`` admit the envelope shape,
  * read-path dispatch (``verify_attestation_record``),
  * consumer regressions (``rebar sign`` render, ``signature_findings``).

Held-out adversarial cases (concurrent first-sign race, verify-never-creates under contention,
read-both coexistence, ssh-keygen-unavailable degrade) live in a separate held-out module.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import rebar
from rebar import signing


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "i")
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.delenv("REBAR_OPCERT_ENV_ID", raising=False)
    rebar.init_repo(repo_root=str(repo))
    return repo


def _tracker(store: Path) -> Path:
    from rebar._commands._seam import tracker_dir

    return Path(tracker_dir(str(store)))


def _signature_event_data(store: Path, tid: str) -> dict:
    """The most-recent persisted SIGNATURE event's ``data`` block (raw, pre-reduce)."""
    resolved = rebar.show_ticket(tid, repo_root=str(store))["ticket_id"]
    tdir = _tracker(store) / resolved
    sig_files = sorted(f for f in os.listdir(tdir) if f.endswith("-SIGNATURE.json"))
    assert sig_files, f"no SIGNATURE event for {tid}"
    payload = json.loads((tdir / sig_files[-1]).read_text(encoding="utf-8"))
    return payload.get("data", payload)


# ── dependency gate (behavioral: assert the e4df reducer/event contract) ──────────────
def test_dependency_gate_import_and_roundtrip(store: Path) -> None:
    from rebar.attest.opcert import opcert_from_record
    from rebar.signing import sign_opcert_manifest

    tid = rebar.create_ticket("task", "dep gate", repo_root=str(store))
    key_path = signing.ensure_opcert_key(str(_tracker(store)))
    principal = signing.opcert_principal(str(_tracker(store)))
    sign_opcert_manifest(
        tid,
        ["plan-review: PASS", "material: m"],
        material_fingerprint="m",
        merged_log_commit="deadbeef",
        key_path=key_path,
        principal=principal,
        repo_root=str(store),
    )
    att = rebar.show_ticket(tid, repo_root=str(store))["attestations"]["plan-review"]
    assert att.get("envelope"), "reducer must pass the envelope through to attestations[kind]"
    assert att.get("merged_log_commit") == "deadbeef"
    env, bound = opcert_from_record(att)
    assert env.signatures and env.signatures[0].keyid == principal
    assert bound["merged_log_commit"] == "deadbeef"


# ── key genesis + permissions + git-ignore + verify-never-creates ─────────────────────
def test_key_genesis_permissions_and_gitignore(store: Path) -> None:
    tracker = _tracker(store)
    tid = rebar.create_ticket("task", "genesis", repo_root=str(store))
    signing.sign_manifest(tid, ["plan-review: PASS"], kind="plan-review", repo_root=str(store))

    key = tracker / ".opcert-key"
    pub = tracker / ".opcert-key.pub"
    assert key.exists() and pub.exists()
    assert oct(key.stat().st_mode & 0o777) == "0o600"
    # passphrase-free Ed25519 (the .pub advertises the ed25519 type).
    assert pub.read_text(encoding="utf-8").startswith("ssh-ed25519 ")
    # git-ignored on the tickets branch.
    gitignore = (tracker / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".opcert-key" in gitignore and ".opcert-key.pub" in gitignore


def test_verify_side_never_creates_key(store: Path) -> None:
    tid = rebar.create_ticket("task", "unsigned", repo_root=str(store))
    result = signing.verify_signature(tid, kind="plan-review", repo_root=str(store))
    assert result["verdict"] == "unsigned"
    assert not (_tracker(store) / ".opcert-key").exists(), "verify must never mint a key"


def test_ensure_opcert_key_verify_mode_raises_without_creating(store: Path) -> None:
    tracker = _tracker(store)
    with pytest.raises(signing.OpcertKeyUnavailable):
        signing.ensure_opcert_key(str(tracker), create_if_missing=False)
    assert not (tracker / ".opcert-key").exists()


def test_missing_pub_is_rederived_from_private_key(store: Path) -> None:
    tracker = _tracker(store)
    tid = rebar.create_ticket("task", "rederive", repo_root=str(store))
    signing.sign_manifest(tid, ["plan-review: PASS"], kind="plan-review", repo_root=str(store))
    pub = tracker / ".opcert-key.pub"
    pub.unlink()  # the .pub is derivative — never a commit point
    # A fresh verify (which reads the pub) re-derives it from the committed private key.
    assert signing.verify_signature(tid, kind="plan-review", repo_root=str(store))["verified"]
    assert pub.exists()


# ── plan-review PASS → envelope SIGNATURE + never-sign guards ─────────────────────────
def test_plan_review_pass_produces_envelope_and_certifies(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar.llm.plan_review import attest

    monkeypatch.setattr(
        "rebar.llm.plan_review.attest.current_material_fingerprint",
        lambda ticket_id, repo_root=None: "fp-static",
    )
    tid = rebar.create_ticket("task", "pr pass", repo_root=str(store))
    verdict = {"verdict": "PASS", "ticket_id": tid, "model": "m", "runner": "pydantic_ai"}
    attest.sign_plan_review(verdict, material="fp-static", repo_root=str(store))

    data = _signature_event_data(store, tid)
    assert data.get("algorithm") == "sshsig" and data.get("envelope")
    assert "signature" not in data  # no HMAC hex
    res = signing.verify_signature(tid, kind="plan-review", repo_root=str(store))
    assert res["verdict"] == "certified" and res["verified"]


def test_plan_review_never_sign_guard_refuses_non_pass(store: Path) -> None:
    from rebar.llm.plan_review import attest

    tid = rebar.create_ticket("task", "block", repo_root=str(store))
    with pytest.raises(signing.SigningError):
        attest.sign_plan_review(
            {"verdict": "BLOCK", "ticket_id": tid}, material="m", repo_root=str(store)
        )
    with pytest.raises(signing.SigningError):
        attest.sign_plan_review(
            {"verdict": "PASS", "ticket_id": tid, "coverage": {"resolution_class": "degraded"}},
            material="m",
            repo_root=str(store),
        )
    # No SIGNATURE event was written.
    resolved = rebar.show_ticket(tid, repo_root=str(store))["ticket_id"]
    tdir = _tracker(store) / resolved
    assert not any(f.endswith("-SIGNATURE.json") for f in os.listdir(tdir))


# ── drift-refresh + resign re-sign as op-certs ────────────────────────────────────────
def test_refresh_attestation_re_signs_as_opcert(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar.llm.plan_review import attest

    monkeypatch.setattr(
        "rebar.llm.plan_review.attest.current_material_fingerprint",
        lambda ticket_id, repo_root=None: "fp-static",
    )
    monkeypatch.setattr("rebar.llm.plan_review.registry.disabled_builtins", lambda repo_root: [])
    tid = rebar.create_ticket("task", "refresh", repo_root=str(store))
    attest.sign_plan_review(
        {"verdict": "PASS", "ticket_id": tid, "model": "m", "runner": "r"},
        material="fp-static",
        repo_root=str(store),
    )
    prior_manifest = signing.verify_signature(tid, kind="plan-review", repo_root=str(store))[
        "manifest"
    ]
    rec = attest.refresh_attestation(tid, prior_manifest, probe="PASS", repo_root=str(store))
    assert rec.get("algorithm") == "sshsig" and rec.get("envelope")
    assert "signature" not in rec  # a refresh never downgrades an op-cert to an HMAC record


def test_resign_plan_review_re_signs_as_opcert(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar.llm.plan_review import resign, sidecar

    monkeypatch.setattr(
        "rebar.llm.plan_review.attest.current_material_fingerprint",
        lambda ticket_id, repo_root=None: "fp-static",
    )
    tid = rebar.create_ticket("task", "resign", repo_root=str(store))
    resolved = rebar.show_ticket(tid, repo_root=str(store))["ticket_id"]
    sidecar.emit(
        {
            "verdict": "PASS",
            "ticket_id": resolved,
            "model": "m",
            "runner": "r",
            "coverage": {},
        },
        material="fp-static",
        repo_root=str(store),
    )
    out = resign.resign_plan_review(tid, repo_root=str(store))
    assert out["ok"] and out["signed"], out
    data = _signature_event_data(store, tid)
    assert data.get("algorithm") == "sshsig" and data.get("envelope")


# ── rebar sign CLI + library seam emit envelopes ──────────────────────────────────────
def test_library_sign_manifest_emits_envelope(store: Path) -> None:
    tid = rebar.create_ticket("task", "lib sign", repo_root=str(store))
    rec = rebar.sign_manifest(tid, ["step one", "step two"], repo_root=str(store))
    assert rec.get("algorithm") == "sshsig" and rec.get("envelope")
    assert "signature" not in rec


def test_rebar_sign_cli_emits_envelope_and_renders(store: Path) -> None:
    tid = rebar.create_ticket("task", "cli sign", repo_root=str(store))
    cp = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "sign", tid, '["ran tests", "lint clean"]'],
        capture_output=True,
        text=True,
        cwd=str(store),
    )
    assert cp.returncode == 0, cp.stderr
    # Text render must not KeyError on the missing HMAC `signature` field.
    assert cp.stdout.startswith("SIGNED ") and "envelope=" in cp.stdout
    data = _signature_event_data(store, tid)
    assert data.get("algorithm") == "sshsig" and data.get("envelope")


def test_rebar_sign_cli_json_validates_against_schema(store: Path) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    pytest.importorskip("referencing")
    from rebar import schemas

    tid = rebar.create_ticket("task", "cli json", repo_root=str(store))
    cp = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "sign", tid, '["a step"]', "--output", "json"],
        capture_output=True,
        text=True,
        cwd=str(store),
    )
    assert cp.returncode == 0, cp.stderr
    out = json.loads(cp.stdout)
    schemas.validator(schemas.SIGN_RESULT).validate(out)
    assert out["envelope"] and out["algorithm"] == "sshsig"
    del jsonschema


# ── completion-verifier signs as an op-cert through the same seam ─────────────────────
def test_completion_verifier_manifest_signs_as_opcert(store: Path) -> None:
    tid = rebar.create_ticket("task", "completion", repo_root=str(store))
    head = signing.head_sha(str(store))
    manifest = [
        "completion-verifier: PASS",
        f"ticket: {tid}",
        "material: fp-c",
        signing.verified_at_sha_step(head),
    ]
    signing.sign_manifest(tid, manifest, kind="completion-verifier", repo_root=str(store))
    data = _signature_event_data(store, tid)
    assert data.get("algorithm") == "sshsig" and data.get("envelope")
    # The bound commit is the manifest's verified-at sha.
    assert data.get("merged_log_commit") == head
    res = signing.verify_signature(tid, kind="completion-verifier", repo_root=str(store))
    assert res["verdict"] == "certified"


# ── expand-phase read-both: legacy HMAC + envelope coexist, kind-keyed ────────────────
def test_read_both_hmac_and_envelope_coexist(store: Path) -> None:
    from rebar._commands._seam import append_event

    tid = rebar.create_ticket("task", "coexist", repo_root=str(store))
    resolved = rebar.show_ticket(tid, repo_root=str(store))["ticket_id"]
    tracker = _tracker(store)
    # A legacy HMAC completion-verifier attestation written the old way (no envelope).
    key = signing.signing_key(str(tracker))
    hmac_manifest = ["completion-verifier: PASS", "material: fp-h"]
    hmac_rec = {
        "manifest": hmac_manifest,
        "algorithm": signing.ALGORITHM,
        "signature": signing.compute_signature(resolved, hmac_manifest, key),
        "key_id": signing.key_fingerprint(key),
        "kind": "completion-verifier",
    }
    append_event(resolved, "SIGNATURE", hmac_rec, tracker, repo_root=str(store))
    # A NEW envelope plan-review attestation on the same ticket.
    signing.sign_manifest(tid, ["plan-review: PASS"], kind="plan-review", repo_root=str(store))

    verdicts = signing.verify_attestations(tid, repo_root=str(store))
    # Kind-keyed coexistence: the legacy HMAC record and the new envelope record BOTH verify.
    assert verdicts["completion-verifier"]["verdict"] == "certified"  # legacy HMAC still verifies
    assert verdicts["completion-verifier"]["algorithm"] == signing.ALGORITHM
    assert verdicts["plan-review"]["verdict"] == "certified"  # new envelope verifies
    assert verdicts["plan-review"]["algorithm"] == "sshsig"
    att = rebar.show_ticket(tid, repo_root=str(store))["attestations"]
    # The op-cert record carries the DSSE envelope; the HMAC record does not.
    assert att["plan-review"].get("envelope") and not att["completion-verifier"].get("envelope")


# ── REBAR_OPCERT_ENV_ID principal override ────────────────────────────────────────────
def test_principal_defaults_to_env_id(store: Path) -> None:
    from rebar._commands._seam import env_id

    tid = rebar.create_ticket("task", "principal default", repo_root=str(store))
    rec = signing.sign_manifest(
        tid, ["plan-review: PASS"], kind="plan-review", repo_root=str(store)
    )
    assert rec["principal"] == env_id(_tracker(store))


def test_principal_override_env_var(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_OPCERT_ENV_ID", "deploy-env-7")
    tid = rebar.create_ticket("task", "principal override", repo_root=str(store))
    rec = signing.sign_manifest(
        tid, ["plan-review: PASS"], kind="plan-review", repo_root=str(store)
    )
    assert rec["principal"] == "deploy-env-7"
    # And it certifies under the same override.
    assert signing.verify_signature(tid, kind="plan-review", repo_root=str(store))["verified"]


# ── schema + SignResultOut admit the envelope shape ───────────────────────────────────
def test_sign_result_schema_admits_envelope_shape(store: Path) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    pytest.importorskip("referencing")
    from rebar import schemas

    tid = rebar.create_ticket("task", "schema", repo_root=str(store))
    rec = rebar.sign_manifest(tid, ["plan-review: PASS"], repo_root=str(store))
    schemas.validator(schemas.SIGN_RESULT).validate(rec)
    del jsonschema


def test_sign_result_out_model_admits_envelope_shape(store: Path) -> None:
    models = pytest.importorskip("rebar._mcp_models")
    if models.SignResultOut is None:  # pydantic unavailable
        pytest.skip("pydantic not installed")
    tid = rebar.create_ticket("task", "model", repo_root=str(store))
    rec = rebar.sign_manifest(tid, ["plan-review: PASS"], repo_root=str(store))
    out = models.SignResultOut.model_validate(rec)
    assert out.envelope and out.algorithm == "sshsig"
    # signature (HMAC) is now optional (expand phase).
    assert not models.SignResultOut.model_fields["signature"].is_required()


# ── read-path dispatch (verify_attestation_record) ────────────────────────────────────
def test_verify_attestation_record_dispatches_on_shape(store: Path) -> None:
    tid = rebar.create_ticket("task", "dispatch", repo_root=str(store))
    resolved = rebar.show_ticket(tid, repo_root=str(store))["ticket_id"]
    tracker = _tracker(store)
    key = signing.signing_key(str(tracker))

    # HMAC-shaped record → routes to the unchanged verify_record HMAC path.
    hmac_manifest = ["plan-review: PASS"]
    hmac_rec = {
        "manifest": hmac_manifest,
        "algorithm": signing.ALGORITHM,
        "signature": signing.compute_signature(resolved, hmac_manifest, key),
        "key_id": signing.key_fingerprint(key),
    }
    hmac_res = signing.verify_attestation_record(hmac_rec, resolved, key=key, repo_root=str(store))
    assert hmac_res["verdict"] == "certified" and hmac_res["algorithm"] == signing.ALGORITHM

    # Envelope-shaped record → routes to the op-cert verifier.
    env_rec = signing.sign_manifest(
        tid, ["plan-review: PASS"], kind="plan-review", repo_root=str(store)
    )
    env_res = signing.verify_attestation_record(env_rec, resolved, repo_root=str(store))
    assert env_res["verdict"] == "certified" and env_res["algorithm"] == "sshsig"


# ── degrade path: ssh-keygen unavailable ──────────────────────────────────────────────
def test_degrade_sign_raises_openssh_remediation(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar.attest import sshsig

    # Simulate a missing/too-old ssh-keygen: ensure_available() (which calls this) then raises,
    # so the seam records the in-band degrade signal — a SigningError naming OpenSSH >= 8.9.
    monkeypatch.setattr(sshsig, "ssh_keygen_version", lambda: None)
    tid = rebar.create_ticket("task", "degrade", repo_root=str(store))
    with pytest.raises(signing.SigningError) as ei:
        signing.sign_manifest(tid, ["plan-review: PASS"], kind="plan-review", repo_root=str(store))
    assert "OpenSSH" in ei.value.message and "8.9" in ei.value.message
    # The operation did not wedge: no SIGNATURE event was written.
    resolved = rebar.show_ticket(tid, repo_root=str(store))["ticket_id"]
    tdir = _tracker(store) / resolved
    assert not any(f.endswith("-SIGNATURE.json") for f in os.listdir(tdir))


def test_claim_gate_blocks_with_openssh_remediation_when_ssh_missing(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar.attest import sshsig

    # Enable the plan-review claim gate, then make ssh-keygen unavailable so no op-cert
    # attestation can be minted: claiming an unsigned ticket must BLOCK and name OpenSSH >= 8.9.
    (store / "rebar.toml").write_text(
        "[verify]\nrequire_plan_review_for_claim = true\n[sync]\npush = 'off'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sshsig, "ssh_keygen_version", lambda: None)
    tid = rebar.create_ticket(
        "task",
        "gate degrade",
        description="Body.\n\n## Acceptance Criteria\n- [ ] a",
        repo_root=str(store),
    )
    with pytest.raises(rebar.RebarError) as ei:
        rebar.claim(tid, assignee="me", repo_root=str(store))
    assert "OpenSSH" in ei.value.stderr and "8.9" in ei.value.stderr


# ── consumer regressions: signature_findings does not mis-report an op-cert as unsigned
def test_signature_findings_certifies_envelope_record(store: Path) -> None:
    from rebar._commands._seam import tracker_dir
    from rebar._engine_support.validate import signature_findings

    tid = rebar.create_ticket("task", "findings", repo_root=str(store))
    signing.sign_manifest(tid, ["plan-review: PASS"], kind="plan-review", repo_root=str(store))
    findings = signature_findings(str(tracker_dir(str(store))))
    # A certified envelope emits NO finding (not a mismatch / foreign / unsigned complaint).
    joined = " ".join(getattr(f, "message", str(f)) for f in findings)
    assert tid[:8] not in joined and "SIGNATURE" not in joined
