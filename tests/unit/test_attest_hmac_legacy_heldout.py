"""Held-out oracle for the HMAC-legacy scheme (task 7e53).

The implementer does NOT see this file. It pins the fail-closed matrix,
idempotent registration, and the no-behavior-change regression on the existing
``rebar.signing`` HMAC path.
"""

from __future__ import annotations

import hashlib
import hmac

from rebar import signing
from rebar.attest import dsse, hmac_legacy, registry


def _hmac_sig(key: bytes, pae_bytes: bytes) -> dsse.Signature:
    return dsse.Signature(keyid="k", sig=hmac.new(key, pae_bytes, hashlib.sha256).digest())


# --- fail-closed matrix -------------------------------------------------------


def test_hmac_wrong_key_is_mismatch() -> None:
    pae_bytes = dsse.pae("t/type", b"body")
    sig = _hmac_sig(b"the-real-key", pae_bytes)
    verdict = hmac_legacy.HmacScheme().verify(pae_bytes, [sig], "rebar-attest-hmac", b"WRONG-key")
    assert verdict.verified is False
    assert verdict.verdict == "mismatch"


def test_hmac_empty_key_fails_closed() -> None:
    # An empty key can never certify (HMAC under an empty key is forgeable).
    pae_bytes = dsse.pae("t/type", b"body")
    sig = _hmac_sig(b"", pae_bytes)
    verdict = hmac_legacy.HmacScheme().verify(pae_bytes, [sig], "rebar-attest-hmac", b"")
    assert verdict.verified is False
    assert verdict.verdict == "foreign_key"


def test_hmac_no_signatures_fails_closed() -> None:
    pae_bytes = dsse.pae("t/type", b"body")
    verdict = hmac_legacy.HmacScheme().verify(pae_bytes, [], "rebar-attest-hmac", b"key")
    assert verdict.verified is False


def test_hmac_near_miss_signature_is_mismatch() -> None:
    # A signature that differs by a single byte still fails (constant-time compare
    # is exact, not prefix-based).
    key = b"env-signing-key"
    pae_bytes = dsse.pae("t/type", b"body")
    good = hmac.new(key, pae_bytes, hashlib.sha256).digest()
    tampered = bytes([good[0] ^ 0x01]) + good[1:]
    verdict = hmac_legacy.HmacScheme().verify(
        pae_bytes, [dsse.Signature(keyid="k", sig=tampered)], "rebar-attest-hmac", key
    )
    assert verdict.verified is False
    assert verdict.verdict == "mismatch"


# --- idempotent registration --------------------------------------------------


def test_register_legacy_schemes_is_idempotent() -> None:
    schemes_backup = dict(registry._SCHEMES)
    policy_backup = dict(registry.POLICY)
    try:
        hmac_legacy.register_legacy_schemes()
        hmac_legacy.register_legacy_schemes()  # second call must not corrupt state
        assert isinstance(registry.get_scheme("HMAC-SHA256"), hmac_legacy.HmacScheme)
        assert registry.resolve("plan-review") == registry.Policy(
            scheme="HMAC-SHA256", namespace="rebar-attest-hmac"
        )
        assert registry.resolve("completion-verifier") == registry.Policy(
            scheme="HMAC-SHA256", namespace="rebar-attest-hmac"
        )
    finally:
        registry._SCHEMES.clear()
        registry._SCHEMES.update(schemes_backup)
        registry.POLICY.clear()
        registry.POLICY.update(policy_backup)


# --- regression: existing signing.py HMAC path is unchanged -------------------


def test_legacy_signing_path_still_certifies() -> None:
    # The pre-existing canonical-payload HMAC verification (signing.verify_record)
    # must keep certifying — this leaf does not touch signing.py's sign/verify.
    key = b"environment-secret-key"
    ticket_id = "t1"
    manifest = ["plan-review: PASS", "ticket: t1", "blocking: 0"]
    sig = signing.compute_signature(ticket_id, manifest, key)
    record = {
        "manifest": manifest,
        "signature": sig,
        "algorithm": signing.ALGORITHM,
        "key_id": signing.key_fingerprint(key),
    }
    verdict = signing.verify_record(record, ticket_id, key)
    assert verdict["verdict"] == "certified"
    assert verdict["verified"] is True
