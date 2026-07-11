"""HMAC-SHA256 as a first-class registry scheme (the legacy/expand phase).

Registers the symmetric HMAC-SHA256 primitive (``rebar.signing``'s algorithm) as
a scheme in the attest registry, keyed by its algorithm id, and pins the legacy
attestation kinds (``plan-review`` / ``completion-verifier``) to it. This is the
**expand** phase of epic brilliant-curly-songbird: the substrate now *knows* HMAC
as a scheme so both consumer epics reference one registry.

Scope boundary: existing legacy attestations (a hex HMAC over the canonical
``{v,algorithm,ticket_id,manifest}`` payload) are NOT rerouted through the
registry — they keep verifying unchanged via ``rebar.signing``. ``HmacScheme``
here is the forward-looking HMAC-**over-DSSE-PAE** scheme.

API STUB — bodies filled by the implementer.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import cast

from rebar import signing
from rebar.attest import dsse, registry

# The domain-separation namespace pinned for HMAC-backed legacy kinds.
HMAC_NAMESPACE = "rebar-attest-hmac"

# The legacy attestation kinds pinned to the HMAC scheme.
LEGACY_KINDS = ("plan-review", "completion-verifier")


class HmacScheme:
    """Symmetric HMAC-SHA256 scheme over DSSE-PAE bytes (``trust_root`` = key)."""

    name = signing.ALGORITHM  # "HMAC-SHA256"

    def verify(
        self,
        pae_bytes: bytes,
        signatures: list[dsse.Signature],
        namespace: str,
        trust_root: object,
    ) -> registry.Verdict:
        # An empty/falsy key is forgeable by anyone, so it can never certify.
        if not trust_root:
            return registry.Verdict(
                verified=False,
                verdict="foreign_key",
                reason="empty HMAC key cannot certify (forgeable)",
            )
        if not signatures:
            return registry.Verdict(
                verified=False,
                verdict="mismatch",
                reason="no signatures to verify",
            )
        key = cast(bytes, trust_root)  # trust_root is the HMAC key (bytes).
        expected = hmac.new(key, pae_bytes, hashlib.sha256).digest()
        if hmac.compare_digest(expected, signatures[0].sig):
            return registry.Verdict(
                verified=True,
                verdict="certified",
                reason="HMAC-SHA256 over DSSE-PAE matches",
            )
        return registry.Verdict(
            verified=False,
            verdict="mismatch",
            reason="HMAC-SHA256 over DSSE-PAE does not match",
        )


def register_legacy_schemes() -> None:
    """Idempotently register :class:`HmacScheme` and pin the legacy-kind policy."""
    registry.register_scheme(HmacScheme())
    for kind in LEGACY_KINDS:
        registry.POLICY[kind] = registry.Policy(
            scheme=HmacScheme.name,
            namespace=HMAC_NAMESPACE,
        )
