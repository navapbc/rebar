"""HELD-OUT oracle for bde5 — the implementation MUST NOT see this file.

Validates the security-critical behaviour the happy path cannot: an envelope
signed by identity A does NOT verify against identity B's keys, a tampered payload
fails, an unknown / keyless identity is non-verified (never raises even if the
lookup itself throws), and the trust root is built only from the identity's in-band
keys. Observable behaviour only (Verdict.verified).
"""

from __future__ import annotations

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


def _signed(store: Path, tmp_path: Path):
    """Sign a payload with Alice's key; return (envelope, alice_id, payload)."""
    priv, pub = _keypair(tmp_path, "alice")
    a = rebar.create_identity("Alice", "alice@example.com", keys=[pub], repo_root=str(store))
    payload = b'{"uuid":"e1","author_id":"' + a.encode() + b'","data":{"x":1}}'
    return authorship.sign_authorship(payload, priv, principal=a), a, payload


def test_cross_identity_rejected(store: Path, tmp_path: Path) -> None:
    """An envelope signed by A does NOT verify against a different identity B."""
    envelope, _a, _payload = _signed(store, tmp_path)
    _priv_b, pub_b = _keypair(tmp_path, "bob")
    b = rebar.create_identity("Bob", "bob@example.com", keys=[pub_b], repo_root=str(store))
    verdict = authorship.verify_authorship(envelope, b, repo_root=str(store))
    assert verdict.verified is False


def test_tampered_payload_rejected(store: Path, tmp_path: Path) -> None:
    """Mutating the signed payload breaks verification against the real author."""
    envelope, a, payload = _signed(store, tmp_path)
    tampered = dsse.Envelope(
        payload_type=envelope.payload_type,
        payload=payload.replace(b'"x":1', b'"x":999'),
        signatures=list(envelope.signatures),
    )
    verdict = authorship.verify_authorship(tampered, a, repo_root=str(store))
    assert verdict.verified is False


def test_tampered_signature_rejected(store: Path, tmp_path: Path) -> None:
    envelope, a, payload = _signed(store, tmp_path)
    bad_sig = bytearray(envelope.signatures[0].sig)
    # Flip a byte in the MIDDLE of the armored signature (the base64 crypto body),
    # not the trailing footer/newline which ssh-keygen tolerates.
    bad_sig[len(bad_sig) // 2] ^= 0xFF
    broken = dsse.Envelope(
        payload_type=envelope.payload_type,
        payload=payload,
        signatures=[dsse.Signature(keyid=envelope.signatures[0].keyid, sig=bytes(bad_sig))],
    )
    assert authorship.verify_authorship(broken, a, repo_root=str(store)).verified is False


def test_unknown_identity_non_verified(store: Path, tmp_path: Path) -> None:
    """Verifying against an id that is not an identity ticket → non-verified, no raise."""
    envelope, _a, _payload = _signed(store, tmp_path)
    task = rebar.create_ticket("task", "not an identity", repo_root=str(store))
    v = authorship.verify_authorship(envelope, task, repo_root=str(store))
    assert v.verified is False


def test_keyless_identity_non_verified(store: Path, tmp_path: Path) -> None:
    """An identity with no keys is a non-verified trust root (never raises)."""
    envelope, _a, _payload = _signed(store, tmp_path)
    keyless = rebar.create_identity("Keyless", "k@example.com", repo_root=str(store))
    assert authorship.verify_authorship(envelope, keyless, repo_root=str(store)).verified is False


def test_verify_never_raises_when_lookup_errors(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the identity lookup itself throws, verify degrades to non-verified."""
    envelope, a, _payload = _signed(store, tmp_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("corrupt store")

    monkeypatch.setattr(rebar, "show_ticket", _boom)
    v = authorship.verify_authorship(envelope, a, repo_root=str(store))
    assert v.verified is False


def test_trust_root_built_from_identity_keys(store: Path, tmp_path: Path) -> None:
    """resolve_trust_root reflects the identity's in-band keys (principal-scoped)."""
    _priv, pub = _keypair(tmp_path, "carol")
    c = rebar.create_identity("Carol", "carol@example.com", keys=[pub], repo_root=str(store))
    tr = authorship.resolve_trust_root(c, repo_root=str(store))
    assert tr is not None
    assert c in tr  # principal appears
    assert pub.split()[1] in tr  # the key data appears
    # An identity we never created has no trust root.
    assert authorship.resolve_trust_root("dead-beef-dead-beef", repo_root=str(store)) is None
