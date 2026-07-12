"""Authorship attestation kind (``rebar.authorship.v1``) on the SSHSIG substrate.

Epic gnu-whale-ichor: an author signs an event payload with their SSH private
key; anyone verifies it against the author's **in-band** public keys (the
``keys`` recorded on that author's ``identity`` ticket) and the pinned
``rebar.authorship.v1`` namespace. Built entirely on the foundation asymmetric
attest substrate — DSSE PAE envelope + the ``sshsig`` scheme + the per-kind
policy table — so it inherits the substrate's fail-closed / no-alg-confusion
guarantees:

* The scheme is pinned by policy (``sshsig``), never chosen from the envelope.
* The trust root is the author identity's own ``keys`` bound to that identity's
  id as the SSHSIG principal — so a payload signed by identity A verifies only
  against A, never against a different identity B (B's keys/principal are a
  disjoint trust root).
* Any identity-lookup problem (unknown id, corrupt store, I/O error, a keyless
  identity) resolves to a non-verified ``Verdict`` — never an exception a caller
  might mistake for a pass.
"""

from __future__ import annotations

from typing import cast

from rebar.attest import dsse, registry, sshsig

AUTHORSHIP_KIND = "rebar.authorship.v1"
AUTHORSHIP_NAMESPACE = "rebar.authorship.v1"
PAYLOAD_TYPE = "application/vnd.rebar.authorship.v1+json"


def register_authorship_policy() -> None:
    """Pin the ``rebar.authorship.v1`` kind to the ``sshsig`` scheme (idempotent)."""
    registry.POLICY[AUTHORSHIP_KIND] = registry.Policy(
        scheme="sshsig",
        namespace=AUTHORSHIP_NAMESPACE,
    )


def sign_authorship(payload: bytes, key_path: str, principal: str) -> dsse.Envelope:
    """Sign ``payload`` as an authorship attestation by ``principal``.

    ``ssh-keygen`` availability is asserted first so signing is honest — a
    missing/too-old ssh-keygen raises :class:`sshsig.SshKeygenUnavailable`
    rather than producing an unverifiable envelope. The signature is taken over
    the DSSE-PAE bytes of ``(PAYLOAD_TYPE, payload)`` under the pinned
    authorship namespace; ``principal`` (the author identity's id) becomes the
    signature ``keyid`` (the SSHSIG principal the verifier binds against).
    """
    sshsig.ensure_available()
    pae = dsse.pae(PAYLOAD_TYPE, payload)
    sig = sshsig.sign(pae, key_path, AUTHORSHIP_NAMESPACE)
    return dsse.Envelope(
        PAYLOAD_TYPE,
        payload,
        [dsse.Signature(keyid=principal, sig=sig)],
    )


def allowed_signers_from_keys(keys: list[str], principal: str) -> str:
    """Render an OpenSSH ``allowed_signers`` file binding ``keys`` to ``principal``.

    Each ``key_line`` is an authorized-keys line (``"ssh-ed25519 AAAA..."``);
    the emitted line is ``"<principal> <key_line>"``. Blank / whitespace-only
    entries are skipped. Lines are joined with a single newline.
    """
    return "\n".join(
        f"{principal} {key_line.strip()}" for key_line in keys if key_line and key_line.strip()
    )


def resolve_trust_root(identity_id: str, *, repo_root=None) -> str | None:
    """Compute the SSHSIG trust root for author ``identity_id``, or ``None``.

    Looks up the identity ticket and, if it is an ``identity`` with a non-empty
    ``keys`` list, returns an ``allowed_signers`` blob binding those keys to the
    identity id as principal. Any lookup problem (unknown id, corrupt store, I/O
    error) or a non-identity / keyless ticket yields ``None`` — this function
    never raises.
    """
    # Import lazily to avoid an import cycle (``rebar`` imports the attest package).
    import rebar

    try:
        ticket = rebar.show_ticket(identity_id, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — any lookup failure (unknown id, corrupt store, I/O) → no trust root, never raise
        return None
    if ticket is None:
        return None
    if ticket.get("ticket_type") != "identity":
        return None
    keys = cast("list[str]", ticket.get("keys") or [])
    if not keys:
        return None
    return allowed_signers_from_keys(keys, principal=identity_id)


def verify_authorship(
    envelope: dsse.Envelope, identity_id: str, *, repo_root=None
) -> registry.Verdict:
    """Verify ``envelope`` as an authorship attestation by ``identity_id``.

    The trust root is resolved from the author identity's own in-band keys; if
    it cannot be resolved (unknown / keyless identity, or any lookup error) the
    result is a non-verified ``Verdict`` with verdict ``"unknown_principal"``.
    This function never raises for an identity-lookup problem.
    """
    trust_root = resolve_trust_root(identity_id, repo_root=repo_root)
    if trust_root is None:
        return registry.Verdict(
            verified=False,
            verdict="unknown_principal",
            reason=(
                f"no verifiable authorship trust root for identity {identity_id!r} "
                "(unknown identity, not an identity ticket, or no keys recorded)"
            ),
        )
    return registry.verify(AUTHORSHIP_KIND, envelope, trust_root)


def authorship_content_hash(event: dict) -> str:
    """The SHA-256 (lowercase hex) digest over the event's canonical JSON bytes,
    with the ``author_sig`` key EXCLUDED (epic gnu-whale-ichor / c96d).

    Signer and verifier derive byte-identical bytes via the repo's canonical
    serializer (sorted-key, compact), so the digest binds an event's content
    independent of dict order and independent of any signature carried on it.
    """
    import hashlib

    from rebar._store.canonical import canonical_str

    payload = canonical_str({k: v for k, v in event.items() if k != "author_sig"}).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_authorship_statement(event_uuid: str, content_hash: str) -> dict:
    """The in-toto Statement an authorship signature wraps (epic gnu-whale-ichor / c96d).

    The single subject binds the event's ``uuid`` (as ``name``) to its content
    digest (``sha256``); the ``predicateType`` is the pinned authorship payload
    type and the ``predicate`` is empty (the binding lives entirely in ``subject``).
    """
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": event_uuid, "digest": {"sha256": content_hash}}],
        "predicateType": PAYLOAD_TYPE,
        "predicate": {},
    }


def sign_event_authorship(event: dict, key_path: str, principal: str) -> dsse.Envelope:
    """Sign ``event``'s authorship as an in-toto Statement (epic gnu-whale-ichor / c96d).

    Wraps ``{event_uuid, content_hash}`` in an in-toto Statement, serializes it to
    canonical JSON, and delegates to the low-level :func:`sign_authorship` primitive
    (which DSSE-envelopes and signs those bytes under the pinned authorship
    namespace). The Statement JSON is the DSSE payload.
    """
    from rebar._store.canonical import canonical_str

    content_hash = authorship_content_hash(event)
    statement = build_authorship_statement(event["uuid"], content_hash)
    payload = canonical_str(statement).encode("utf-8")
    return sign_authorship(payload, key_path, principal)


def verify_event_authorship(
    event: dict, envelope: dsse.Envelope, identity_id: str, *, repo_root=None
) -> registry.Verdict:
    """Verify ``envelope`` is a valid authorship Statement over ``event`` by ``identity_id``.

    Three fail-closed gates: (1) the DSSE payload parses as an in-toto Statement;
    (2) its subject binds this event's ``uuid`` AND content hash; (3) the DSSE
    signature verifies against the author identity's in-band keys (delegated to
    :func:`verify_authorship`). Any parse/shape problem yields a non-verified
    ``Verdict`` — this function never raises for a lookup/parse problem.
    """
    import json

    try:
        statement = json.loads(envelope.payload.decode("utf-8"))
        subject = statement["subject"]
        if not isinstance(subject, list) or not subject:
            raise ValueError("empty or non-list subject")
        first = subject[0]
        subject_name = first["name"]
        subject_hash = first["digest"]["sha256"]
    except Exception:  # noqa: BLE001 — malformed / non-Statement payload → non-verified, never raise
        return registry.Verdict(
            verified=False,
            verdict="malformed_statement",
            reason="envelope payload is not a valid in-toto authorship Statement",
        )

    if subject_name != event["uuid"] or subject_hash != authorship_content_hash(event):
        return registry.Verdict(
            verified=False,
            verdict="subject_mismatch",
            reason="Statement subject does not bind this event's uuid and content hash",
        )

    return verify_authorship(envelope, identity_id, repo_root=repo_root)
