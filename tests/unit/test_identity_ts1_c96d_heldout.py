"""HELD-OUT oracle for TS1 (c96d) — the implementation MUST NOT see this file.

Validates the security-critical + contract behaviour the happy path cannot:
- cross-identity rejection, tampered body (content_hash mismatch), tampered signature;
- a malformed / non-Statement payload is non-verified (never raises);
- an unknown / keyless identity is non-verified;
- ``authorship_content_hash`` excludes ``author_sig`` and is sensitive to every other field;
- the low-level ``sign_authorship``/``verify_authorship`` primitive is PRESERVED (KEY-op path).
Observable behaviour only.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar.attest import authorship, dsse, sshsig

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001 — best-effort SSHSIG availability probe; skip if unavailable
    _SSH_OK = False

pytestmark = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "dev@example.com"),
        ("git", "config", "user.name", "Dev"),
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


def _event(author_id: str) -> dict:
    return {
        "uuid": "e1-1111-2222-3333",
        "event_type": "COMMENT",
        "author_id": author_id,
        "timestamp": "0000",
        "data": {"body": "hello"},
    }


# ── content_hash contract ─────────────────────────────────────────────────────
def test_content_hash_excludes_author_sig() -> None:
    """Adding/removing an author_sig key does not change the content_hash (the
    signature never covers itself); the hash is lowercase-hex sha256."""
    event = _event("id-a")
    base = authorship.authorship_content_hash(event)
    with_sig = dict(event, author_sig="anything-at-all")
    assert authorship.authorship_content_hash(with_sig) == base
    assert len(base) == 64 and all(c in "0123456789abcdef" for c in base)


def test_content_hash_sensitive_to_other_fields() -> None:
    """Mutating any non-author_sig field changes the content_hash."""
    event = _event("id-a")
    base = authorship.authorship_content_hash(event)
    mutated = dict(event, data={"body": "tampered"})
    assert authorship.authorship_content_hash(mutated) != base


# ── verify_event_authorship security ──────────────────────────────────────────
def test_cross_identity_rejected(store: Path, tmp_path: Path) -> None:
    """An event signed by A does NOT verify against a different identity B."""
    priv, pub = _keypair(tmp_path, "alice")
    a = rebar.create_identity("Alice", "alice@example.com", keys=[pub], repo_root=str(store))
    event = _event(a)
    envelope = authorship.sign_event_authorship(event, priv, principal=a)

    _priv_b, pub_b = _keypair(tmp_path, "bob")
    b = rebar.create_identity("Bob", "bob@example.com", keys=[pub_b], repo_root=str(store))
    verdict = authorship.verify_event_authorship(event, envelope, b, repo_root=str(store))
    assert verdict.verified is False


def test_tampered_body_fails(store: Path, tmp_path: Path) -> None:
    """A signature bound to the original event does not verify a tampered event
    (subject content_hash no longer matches)."""
    priv, pub = _keypair(tmp_path, "alice")
    a = rebar.create_identity("Alice", "alice@example.com", keys=[pub], repo_root=str(store))
    event = _event(a)
    envelope = authorship.sign_event_authorship(event, priv, principal=a)

    tampered = dict(event, data={"body": "MALICIOUS"})
    verdict = authorship.verify_event_authorship(tampered, envelope, a, repo_root=str(store))
    assert verdict.verified is False


def test_tampered_signature_fails(store: Path, tmp_path: Path) -> None:
    """Flipping a byte in the signature makes verification fail (not raise)."""
    priv, pub = _keypair(tmp_path, "alice")
    a = rebar.create_identity("Alice", "alice@example.com", keys=[pub], repo_root=str(store))
    event = _event(a)
    envelope = authorship.sign_event_authorship(event, priv, principal=a)

    sig = bytearray(envelope.signatures[0].sig)
    sig[len(sig) // 2] ^= 0xFF  # flip a middle byte (armor footer tolerated)
    broken = dsse.Envelope(
        envelope.payload_type,
        envelope.payload,
        [dsse.Signature(keyid=envelope.signatures[0].keyid, sig=bytes(sig))],
    )
    verdict = authorship.verify_event_authorship(event, broken, a, repo_root=str(store))
    assert verdict.verified is False


def test_subject_name_must_match_event_uuid(store: Path, tmp_path: Path) -> None:
    """verify_event_authorship enforces the subject[0].name == event uuid clause
    INDEPENDENTLY of the digest clause: a properly-signed Statement whose digest is
    correct but whose subject name is wrong does not verify."""
    priv, pub = _keypair(tmp_path, "alice")
    a = rebar.create_identity("Alice", "alice@example.com", keys=[pub], repo_root=str(store))
    event = _event(a)

    # Hand-build a Statement with the CORRECT content_hash but a WRONG subject name,
    # then sign it with A's real key (so only the name clause can reject it).
    correct_hash = authorship.authorship_content_hash(event)
    bad_stmt = authorship.build_authorship_statement("WRONG-uuid-0000", correct_hash)
    payload = json.dumps(bad_stmt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    envelope = authorship.sign_authorship(payload, priv, principal=a)

    verdict = authorship.verify_event_authorship(event, envelope, a, repo_root=str(store))
    assert verdict.verified is False


def test_malformed_statement_non_verified(store: Path, tmp_path: Path) -> None:
    """A DSSE envelope whose payload is NOT a valid in-toto Statement is
    non-verified — never raises."""
    priv, pub = _keypair(tmp_path, "alice")
    a = rebar.create_identity("Alice", "alice@example.com", keys=[pub], repo_root=str(store))
    event = _event(a)
    # A syntactically valid envelope, but the payload is not a Statement (no subject).
    bogus = authorship.sign_authorship(
        json.dumps({"not": "a-statement"}).encode("utf-8"), priv, principal=a
    )
    verdict = authorship.verify_event_authorship(event, bogus, a, repo_root=str(store))
    assert verdict.verified is False


def test_unknown_identity_non_verified(store: Path, tmp_path: Path) -> None:
    """An unknown author identity yields a non-verified verdict, never an exception."""
    priv, pub = _keypair(tmp_path, "alice")
    a = rebar.create_identity("Alice", "alice@example.com", keys=[pub], repo_root=str(store))
    event = _event(a)
    envelope = authorship.sign_event_authorship(event, priv, principal=a)
    verdict = authorship.verify_event_authorship(
        event, envelope, "nonexistent-identity-id", repo_root=str(store)
    )
    assert verdict.verified is False


# ── primitive preservation (KEY-op path unaffected) ───────────────────────────
def test_low_level_primitive_preserved(store: Path, tmp_path: Path) -> None:
    """The low-level sign_authorship/verify_authorship primitive still signs and
    verifies raw payload bytes (the KEY-op signing path), unchanged by the
    Statement layer."""
    priv, pub = _keypair(tmp_path, "alice")
    a = rebar.create_identity("Alice", "alice@example.com", keys=[pub], repo_root=str(store))
    payload = b'{"op":"KEY_ADD","identity_id":"' + a.encode() + b'","public_key":"k"}'
    envelope = authorship.sign_authorship(payload, priv, principal=a)
    # raw-bytes payload preserved (NOT wrapped in a Statement)
    assert envelope.payload == payload
    verdict = authorship.verify_authorship(envelope, a, repo_root=str(store))
    assert verdict.verified is True
