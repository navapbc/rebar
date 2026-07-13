"""Happy-path spec for op-cert storage on a ticket (keystone e4df / behavioral-winsome-blacklemur).

The ONLY tests the implementation subagent sees. Pins the approved Option-A design:
* `signing.sign_opcert_manifest(...)` writes an envelope-bearing SIGNATURE event, and the ticket's
  reduced `attestations[<kind>]` record then carries the encoded DSSE `envelope` + bound
  `material_fingerprint`/`merged_log_commit`;
* `opcert.opcert_from_record(record)` round-trips that envelope, which verifies against a pinned
  keyring.

Held-out (compaction survival, legacy-HMAC → None, additive-field invariance) lives in
`test_opcert_storage_heldout.py`. Real ssh-keygen + a real rebar store (git-backed).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import signing
from rebar.attest import opcert, sshsig

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001
    _SSH_OK = False

pytestmark = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")

ENV_ID = "trusted-ci@rebar.test"
MATERIAL = "0123456789abcdef"
KIND = "completion-verifier"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "9" * 18)
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "d@e.test"),
        ("git", "config", "user.name", "D"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _keypair(tmp_path: Path, name: str) -> tuple[str, str]:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", name],
        check=True,
        capture_output=True,
    )
    parts = (tmp_path / f"{name}.pub").read_text().strip().split()
    return str(key), f"{parts[0]} {parts[1]}"


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_sign_opcert_stores_envelope_on_ticket(store: Path, tmp_path: Path) -> None:
    """The producer writes an envelope-bearing SIGNATURE event; the ticket's reduced
    attestations[kind] record carries the encoded envelope + bound fields, and no HMAC."""
    priv, _pub = _keypair(tmp_path, "env")
    commit = _head(store)
    tid = rebar.create_ticket("task", "op-cert me", repo_root=str(store))

    signing.sign_opcert_manifest(
        tid,
        [f"{KIND}: PASS", f"ticket: {tid}"],
        material_fingerprint=MATERIAL,
        merged_log_commit=commit,
        key_path=priv,
        principal=ENV_ID,
        repo_root=str(store),
    )

    state = rebar.show_ticket(tid, repo_root=str(store))
    rec = state["attestations"][KIND]
    assert rec.get("envelope")  # non-empty encoded DSSE envelope
    assert rec["material_fingerprint"] == MATERIAL
    assert rec["merged_log_commit"] == commit
    assert not rec.get("signature")  # asymmetric op-cert carries no HMAC


def test_opcert_from_record_roundtrips_and_verifies(store: Path, tmp_path: Path) -> None:
    """The read helper reconstructs the envelope, which verifies against the pinned keyring."""
    priv, pub = _keypair(tmp_path, "env")
    commit = _head(store)
    tid = rebar.create_ticket("task", "op-cert me", repo_root=str(store))
    signing.sign_opcert_manifest(
        tid,
        [f"{KIND}: PASS"],
        material_fingerprint=MATERIAL,
        merged_log_commit=commit,
        key_path=priv,
        principal=ENV_ID,
        repo_root=str(store),
    )

    rec = rebar.show_ticket(tid, repo_root=str(store))["attestations"][KIND]
    got = opcert.opcert_from_record(rec)
    assert got is not None
    envelope, bound = got
    assert bound["material_fingerprint"] == MATERIAL
    assert bound["merged_log_commit"] == commit

    keyring = [{"public_key": pub, "added_at_commit": commit, "revoked_at_commit": None}]
    verdict = opcert.verify_opcert(
        envelope, tid, MATERIAL, commit, keyring, kind=KIND, principal=ENV_ID, repo_root=str(store)
    )
    assert verdict.verified is True
    assert verdict.verdict == "certified"
