"""Pluggable scheme registry + per-kind policy table + verify-dispatch.

The domain-separation core of the attestation substrate (epic
brilliant-curly-songbird). A **static, source-pinned** policy table maps each
attestation ``kind`` to a ``Policy(scheme, namespace)``. ``verify(kind, envelope,
trust_root)`` resolves that policy and dispatches to the pinned scheme — it
**never** selects the scheme from the (attacker-controllable) envelope. This
closes the JWT ``alg``-confusion class (RFC 8725): the verifier's choice of
algorithm/scheme cannot be influenced by record data.

Security properties (pinned by tests):

* Scheme selection comes from ``POLICY[kind].scheme`` only — never from the
  envelope's ``keyid``/signature contents.
* An unknown ``kind`` or an unknown policy ``scheme`` fails **closed** (a
  non-verified ``Verdict``), never an exception that a caller might treat as pass.
* The scheme is invoked over the DSSE-PAE bytes of the envelope and the
  policy-pinned ``namespace``; ``trust_root`` (which keys to trust) is a
  caller/deployment concern, supplied at the call site.

API STUB — signatures are pinned here so the SSHSIG (56d9) and HMAC-legacy
(7e53) schemes can register against a stable surface; bodies are filled by the
implementer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from rebar.attest import dsse


@dataclass(frozen=True)
class Verdict:
    """Uniform verify outcome (mirrors ``signing.verify_record``'s contract)."""

    verified: bool
    verdict: str  # "certified" | "mismatch" | "unknown_kind" | "unknown_scheme" | ...
    reason: str


@dataclass(frozen=True)
class Policy:
    """Out-of-band-pinned policy for an attestation kind."""

    scheme: str
    namespace: str


class Scheme(Protocol):
    """A signing scheme plugged into the registry (SSHSIG, HMAC-legacy, ...)."""

    name: str

    def verify(
        self,
        pae_bytes: bytes,
        signatures: list[dsse.Signature],
        namespace: str,
        trust_root: Any,
    ) -> Verdict: ...


# The scheme registry (name -> Scheme) and the static per-kind policy table.
# POLICY is a source-pinned module-level dict — deliberately NOT loaded from
# config/env, because attacker/config control over scheme selection is exactly
# the threat this table exists to remove. Later leaves populate both.
_SCHEMES: dict[str, Scheme] = {}
POLICY: dict[str, Policy] = {}


def register_scheme(scheme: Scheme) -> None:
    """Register ``scheme`` into the module-level registry, keyed by its name."""
    _SCHEMES[scheme.name] = scheme


def get_scheme(name: str) -> Scheme | None:
    """Return the registered scheme named ``name``, or ``None`` if absent."""
    return _SCHEMES.get(name)


def resolve(kind: str) -> Policy | None:
    """Return the pinned :class:`Policy` for ``kind``, or ``None`` if absent."""
    return POLICY.get(kind)


def verify(kind: str, envelope: dsse.Envelope, trust_root: Any) -> Verdict:
    """Verify ``envelope`` under the policy pinned for ``kind`` (fails closed).

    The scheme is selected **only** from ``POLICY[kind].scheme`` — never from the
    (attacker-controllable) envelope — which closes the alg-confusion class. An
    unknown kind or unknown policy scheme returns a non-verified ``Verdict``
    rather than raising.
    """
    policy = POLICY.get(kind)
    if policy is None:
        return Verdict(
            verified=False,
            verdict="unknown_kind",
            reason=f"no policy pinned for kind {kind!r}",
        )
    scheme = _SCHEMES.get(policy.scheme)
    if scheme is None:
        return Verdict(
            verified=False,
            verdict="unknown_scheme",
            reason=f"policy scheme {policy.scheme!r} for kind {kind!r} is not registered",
        )
    return scheme.verify(
        envelope.pae(),
        list(envelope.signatures),
        policy.namespace,
        trust_root,
    )
