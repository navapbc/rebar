"""Tests for the `rebar remote-cert` client (story ee0b).

Offline: no network / AWS. The transport (SigV4 + HTTP) is never exercised here; these pin the
PERSIST contract — the returned envelope is stored as a `SIGNATURE` event the op-cert verifier
certifies, a tampered bound value is rejected (`mismatch`), and a non-PASS/error verdict exits
non-zero — plus the offline-default guard on `verify.opcert_remote_url`.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from _opcert_helpers import keypair, store_with_chain

import rebar
from rebar._commands import remote_cert
from rebar.attest import dsse, opcert, sshsig

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001 — availability probe
    _SSH_OK = False

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required"),
]

ENV_ID = "nava-opcert-prod-1"
KIND = "completion-verifier"
MATERIAL = "0123456789abcdef"
MERGED = "0" * 40


def _a_ticket_id(tracker: str) -> str:
    dirs = sorted(
        d
        for d in os.listdir(tracker)
        if os.path.isdir(os.path.join(tracker, d)) and not d.startswith(".")
    )
    assert dirs, "store has no ticket dirs"
    return dirs[0]


def _service_job(ticket_id: str, priv: str) -> dict:
    """Simulate the trusted service's returned job: a real op-cert envelope + bound fields."""
    env = opcert.sign_opcert(
        ticket_id, MATERIAL, MERGED, key_path=priv, kind=KIND, principal=ENV_ID
    )
    encoded = dsse.encode(
        env.payload_type, env.payload, [{"keyid": s.keyid, "sig": s.sig} for s in env.signatures]
    )
    return {
        "status": "completed",
        "verdict": "PASS",
        "kind": KIND,
        "envelope": encoded,
        "material_fingerprint": MATERIAL,
        "merged_log_commit": MERGED,
        "manifest": [f"{KIND}: PASS", f"ticket: {ticket_id}"],
    }


def test_persist_envelope_roundtrips_and_certifies(tmp_path, monkeypatch):
    repo, tracker, pos = store_with_chain(tmp_path, monkeypatch, 3)
    tid = _a_ticket_id(tracker)
    priv, pub = keypair(tmp_path, "env")
    job = _service_job(tid, priv)

    resolved = remote_cert.persist_envelope(job, tid, KIND, repo_root=str(repo))

    # The persisted SIGNATURE event round-trips: bound fields read from the SIGNED payload.
    rec = rebar.show_ticket(resolved, repo_root=str(repo))["attestations"][KIND]
    decoded = opcert.opcert_from_record(rec)
    assert decoded is not None
    _env, bound = decoded
    assert bound["material_fingerprint"] == MATERIAL
    assert bound["merged_log_commit"] == MERGED

    # `rebar verify-opcert`'s core certifies it against the pinned keyring at the storage anchor.
    keyring = [
        {"public_key": pub, "added_at_log_position": pos[0][0], "revoked_at_log_position": None}
    ]
    envelope = dsse.decode(rec["envelope"])
    verdict = opcert.verify_opcert(
        envelope,
        resolved,
        MATERIAL,
        MERGED,
        keyring,
        kind=KIND,
        principal=ENV_ID,
        storage_anchor_commit=pos[-1][1],
        storage_anchor_position=pos[-1][0],
        repo_root=str(repo),
    )
    assert verdict.verified is True
    assert verdict.verdict == "certified"


def test_tampered_bound_value_is_rejected(tmp_path, monkeypatch):
    repo, tracker, pos = store_with_chain(tmp_path, monkeypatch, 3)
    tid = _a_ticket_id(tracker)
    priv, pub = keypair(tmp_path, "env")
    job = _service_job(tid, priv)
    resolved = remote_cert.persist_envelope(job, tid, KIND, repo_root=str(repo))
    rec = rebar.show_ticket(resolved, repo_root=str(repo))["attestations"][KIND]

    keyring = [
        {"public_key": pub, "added_at_log_position": pos[0][0], "revoked_at_log_position": None}
    ]
    envelope = dsse.decode(rec["envelope"])
    # A doctored material fingerprint no longer matches the SIGNED subject digest → mismatch.
    verdict = opcert.verify_opcert(
        envelope,
        resolved,
        "deadbeefdeadbeef",  # tampered material
        MERGED,
        keyring,
        kind=KIND,
        principal=ENV_ID,
        storage_anchor_commit=pos[-1][1],
        storage_anchor_position=pos[-1][0],
        repo_root=str(repo),
    )
    assert verdict.verified is False
    assert verdict.verdict == "mismatch"


def test_finalize_nonpass_exits_nonzero(tmp_path, monkeypatch):
    repo, tracker, _pos = store_with_chain(tmp_path, monkeypatch, 1)
    tid = _a_ticket_id(tracker)
    # A FAIL verdict → no persist, non-zero.
    assert (
        remote_cert.finalize(
            {"status": "completed", "verdict": "FAIL"}, tid, KIND, repo_root=str(repo)
        )
        == 1
    )
    # An error status → non-zero.
    assert (
        remote_cert.finalize(
            {"status": "error", "verdict": None, "error": {"class": "llm_error", "message": "x"}},
            tid,
            KIND,
            repo_root=str(repo),
        )
        == 1
    )


def test_finalize_pass_persists_and_exits_zero(tmp_path, monkeypatch):
    repo, tracker, _pos = store_with_chain(tmp_path, monkeypatch, 2)
    tid = _a_ticket_id(tracker)
    priv, _pub = keypair(tmp_path, "env")
    job = _service_job(tid, priv)
    rc = remote_cert.finalize(job, tid, KIND, repo_root=str(repo))
    assert rc == 0
    rec = rebar.show_ticket(tid, repo_root=str(repo))["attestations"][KIND]
    assert rec.get("envelope")


def test_remote_cert_cli_errors_when_url_unset(tmp_path, monkeypatch):
    """Unset verify.opcert_remote_url → a clear error, exit 2 (never required for local ops)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    for args in (
        ("config", "user.email", "t@e.test"),
        ("config", "user.name", "t"),
        ("commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    tid = rebar.create_ticket("task", "t", description="d" * 250, repo_root=str(repo))
    rc = remote_cert.cli([tid, KIND, "--root", str(repo)])
    assert rc == 2
