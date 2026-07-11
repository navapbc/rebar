"""Happy-path spec for the HMAC-legacy registry scheme (task 7e53).

Edge cases (fail-closed matrix, idempotency, signing.py regression) live in the
held-out companion ``test_attest_hmac_legacy_heldout.py``.
"""

from __future__ import annotations

import hashlib
import hmac

from rebar.attest import dsse, hmac_legacy, registry


def _hmac_sig(key: bytes, pae_bytes: bytes) -> dsse.Signature:
    return dsse.Signature(keyid="k", sig=hmac.new(key, pae_bytes, hashlib.sha256).digest())


def test_hmac_scheme_registered_and_policy_pinned() -> None:
    # register_legacy_schemes() runs at package import; the scheme + policy are live.
    scheme = registry.get_scheme("HMAC-SHA256")
    assert isinstance(scheme, hmac_legacy.HmacScheme)

    for kind in ("plan-review", "completion-verifier"):
        policy = registry.resolve(kind)
        assert policy is not None
        assert policy.scheme == "HMAC-SHA256"
        assert policy.namespace == "rebar-attest-hmac"


def test_hmac_scheme_certifies_correct_key() -> None:
    key = b"env-signing-key-0001"
    pae_bytes = dsse.pae("t/type", b"attestation body bytes")
    sig = _hmac_sig(key, pae_bytes)

    verdict = hmac_legacy.HmacScheme().verify(pae_bytes, [sig], "rebar-attest-hmac", key)

    assert verdict.verified is True
    assert verdict.verdict == "certified"
