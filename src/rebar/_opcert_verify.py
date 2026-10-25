"""Verify DSSE op-certs, their signed subjects, and environment trust roots.

Mint and verify live in separate call clusters. Custody helpers load lazily to
avoid a cycle with ``_opcert_signing`` and its verifier re-export.
"""

from __future__ import annotations

import json

from rebar import config


def _opcert_subject_binding_error(
    envelope, bound: dict, ticket_id: str, expected_kind
) -> dict | None:
    """Reject signed-subject digest, ticket, or kind mismatches.

    The caller has verified the DSSE signature. This check prevents replay of a
    valid cert across tickets or attestation slots.
    """
    from rebar.attest import opcert

    bound_ticket = bound.get("ticket_id")
    bound_kind = bound.get("kind")
    try:
        # ``envelope.payload`` may be bytes or str depending on the dsse codec; json.loads accepts
        # both, so decode defensively.
        payload = envelope.payload
        statement = json.loads(payload.decode("utf-8") if isinstance(payload, bytes) else payload)
        subject = statement["subject"]
        if not isinstance(subject, list) or not subject:
            raise ValueError("empty or non-list subject")
        subject_name = subject[0]["name"]
        subject_hash = subject[0]["digest"]["sha256"]
    except Exception:  # noqa: BLE001 — malformed / non-Statement payload → mismatch, never raise
        return {
            "verified": False,
            "verdict": "mismatch",
            "reason": "op-cert envelope payload is not a valid in-toto op-cert Statement",
        }
    # Consistency: the predicate we extract from must agree with the SIGNED subject digest.
    expected_digest = opcert.opcert_subject_digest(
        bound_ticket or "",
        bound.get("material_fingerprint") or "",
        bound.get("merged_log_commit") or "",
        bound_kind or "",
    )
    if subject_name != bound_ticket or subject_hash != expected_digest:
        return {
            "verified": False,
            "verdict": "mismatch",
            "reason": "op-cert subject digest does not match its signed predicate",
        }
    # Binding: the signed subject must name the ticket being verified …
    if bound_ticket != ticket_id:
        return {
            "verified": False,
            "verdict": "mismatch",
            "reason": (
                f"op-cert is bound to ticket {bound_ticket!r}, not {ticket_id!r} "
                f"(cross-ticket replay)"
            ),
        }
    # … and the attestation kind slot being verified (kind-confusion defense).
    if expected_kind is not None and bound_kind != expected_kind:
        return {
            "verified": False,
            "verdict": "mismatch",
            "reason": (
                f"op-cert is bound to kind {bound_kind!r}, not {expected_kind!r} "
                f"(cross-kind replay)"
            ),
        }
    return None


def _required_environment(repo_root) -> str | None:
    """Read the optional required environment; default to no identity gate.

    The merged-log enforcement boundary belongs to the merge gate, not this
    per-record check. Unreadable config cannot enable a restriction.
    """
    try:
        return config.compose_config(root=repo_root).verify.require_environment or None
    except Exception:  # noqa: BLE001 — no readable config ⇒ no required environment
        return None


def _pinned_environment_keys(
    principal: str, repo_root, *, unrevoked_only: bool = False
) -> list[str]:
    """Public keys pinned for ``principal`` in ``.rebar/trusted_environments.yaml`` (the
    out-of-band, review-gated trust root the ``verify-opcert`` merge gate uses). Empty when the
    environment is not pinned, or the config is absent/malformed. ``unrevoked_only`` drops
    entries carrying a ``revoked_at_log_position``."""
    try:
        from rebar.attest import trusted_env

        keyring = trusted_env.trusted_env_keyring(principal, repo_root) or []
    except Exception:  # noqa: BLE001 — absent/malformed pin config ⇒ nothing pinned here
        return []
    return [
        rec["public_key"]
        for rec in keyring
        if isinstance(rec, dict)
        and isinstance(rec.get("public_key"), str)
        and rec["public_key"]
        and not (unrevoked_only and rec.get("revoked_at_log_position"))
    ]


def _opcert_trust_root(
    principal: str, own_principal: str, tracker: str, envelope, repo_root, required_env: str | None
) -> tuple[list[str], str | None]:
    """Choose own, pinned, or embedded signer keys and label the trust basis.

    Embedded keys prove signature consistency but not known environment identity;
    required-environment policy disables that fallback.
    """
    from rebar._opcert_signing import _opcert_own_public_keys

    is_own = principal == own_principal
    if is_own:
        own_pubs = _opcert_own_public_keys(tracker)
        if own_pubs:
            return own_pubs, "own_key"
    # A revoked pinned key is never trusted, for the own or a foreign principal (bugs d909,
    # 0ed1). There is no era check: a certificate signed before the key's revocation is refused
    # under verify.require_environment, and in advisory mode verifies on its embedded key.
    pinned = _pinned_environment_keys(principal, repo_root, unrevoked_only=True)
    if pinned:
        return pinned, "pinned_environment"
    if required_env is not None:
        return [], None
    from rebar.attest import sshsig

    sigs = list(envelope.signatures)
    embedded = sshsig.embedded_public_key(sigs[0].sig) if sigs else None
    return ([embedded], "envelope_key") if embedded else ([], None)


def _rotated_own_key_verdict(envelope, principal: str, repo_root):
    """Retry an own-principal ``mismatch`` under the pinned, unrevoked keys of that principal.

    After the own key rotates, certificates signed under the previous key still carry the same
    principal. Trust is "pinned and not revoked", with no era check: this function has no
    tickets-log position for the certificate, so a key pinned after the certificate was signed
    is not rejected here. Returns the verified verdict, or None when no pinned key verifies it.
    """
    from rebar.attest import authorship, registry
    from rebar.attest.opcert import OPCERT_KIND

    pinned = _pinned_environment_keys(principal, repo_root, unrevoked_only=True)
    if not pinned:
        return None
    retry = registry.verify(
        OPCERT_KIND, envelope, authorship.allowed_signers_from_keys(pinned, principal)
    )
    return retry if retry.verified else None


def verify_opcert_record(
    record: dict, ticket_id: str, *, kind: str | None = None, repo_root=None
) -> dict:
    """Verify a DSSE record under the strongest available trust basis.

    Environment identity is advisory unless ``verify.require_environment`` is set.
    Signature, principal, and signed ticket/kind binding still gate certification.
    Return authenticated material, commit, and manifest for freshness checks;
    verification never creates a key.
    """
    from rebar._opcert_signing import opcert_principal
    from rebar.attest import authorship, opcert, registry
    from rebar.attest.opcert import OPCERT_KIND
    from rebar.reducer._processors import attestation_kind
    from rebar.signing import rebar_version_from_manifest, verified_at_sha_from_manifest

    raw_manifest = record.get("manifest")
    manifest = raw_manifest if isinstance(raw_manifest, list) else []
    tracker = str(config.tracker_dir(repo_root))
    own_principal = opcert_principal(tracker)

    decoded = opcert.opcert_from_record(record)
    envelope = decoded[0] if decoded is not None else None
    bound = decoded[1] if decoded is not None else {}
    principal = record.get("principal")
    if not principal and envelope is not None and envelope.signatures:
        principal = envelope.signatures[0].keyid

    base = {
        "manifest": manifest,
        "step_count": len(manifest),
        "algorithm": record.get("algorithm"),
        # key_id carries the record's principal (the op-cert's identity), mirroring the HMAC
        # record's key fingerprint slot so provenance reads keep working.
        "key_id": principal or None,
        "signed_at": record.get("signed_at"),
        "head_sha": record.get("head_sha"),
        # verified_at_sha is the EXPLICIT manifest pin only (None when unpinned) — the bound
        # merged_log_commit is surfaced separately below.
        "verified_at_sha": (
            verified_at_sha_from_manifest(manifest) or record.get("verified_at_sha")
        ),
        "rebar_version": rebar_version_from_manifest(manifest),
        # Freshness reads signed material and commit, not the mutable record mirror.
        "material_fingerprint": bound.get("material_fingerprint"),
        "merged_log_commit": bound.get("merged_log_commit"),
        # Prefer the signed manifest for stale-code and stale-registry checks;
        # legacy certs without one fall back to their sole plaintext manifest.
        "signed_manifest": bound.get("manifest"),
        # This verified path, not the mutable algorithm field, marks an op-cert.
        "opcert": True,
        # Expose own, pinned, or embedded-key trust; the last does not establish
        # a known environment and must remain visible to audit readers.
        "trust_basis": None,
    }

    if envelope is None:
        # A malformed / undecodable envelope fails CLOSED (never certified).
        return {
            **base,
            "verified": False,
            "verdict": "invalid",
            "reason": "op-cert envelope could not be decoded",
        }

    # A cert with no principal at all names no signer — there is nothing to bind a key to.
    if not principal:
        return {
            **base,
            "verified": False,
            "verdict": "foreign_key",
            "reason": "op-cert carries no principal; there is no identity to verify it under",
        }

    # The OPT-IN future feature (deferred by operator policy, unset in this project): when an
    # operator sets ``verify.require_environment``, the trusted set IS restricted again.
    required_env = _required_environment(repo_root)
    if required_env is not None and principal != required_env:
        return {
            **base,
            "verified": False,
            "verdict": "foreign_key",
            "reason": (
                f"verify.require_environment is set to {required_env!r}, but this op-cert was "
                f"signed by environment {principal!r}"
            ),
        }

    keys, trust_basis = _opcert_trust_root(
        principal, own_principal, tracker, envelope, repo_root, required_env
    )
    base = {**base, "trust_basis": trust_basis}
    if not keys:
        return {
            **base,
            "verified": False,
            "verdict": "foreign_key",
            "reason": (
                f"no public key is obtainable for environment {principal!r} — neither this "
                "environment's own key, nor a key pinned in .rebar/trusted_environments.yaml, "
                "nor one embedded in the signature envelope"
            ),
        }

    trust_root = authorship.allowed_signers_from_keys(keys, principal)
    verdict = registry.verify(OPCERT_KIND, envelope, trust_root)
    if not verdict.verified and verdict.verdict == "mismatch" and trust_basis == "own_key":
        rotated = _rotated_own_key_verdict(envelope, principal, repo_root)
        if rotated is not None:
            verdict, base = rotated, {**base, "trust_basis": "pinned_environment"}
    if not verdict.verified:
        return {
            **base,
            "verified": verdict.verified,
            "verdict": verdict.verdict,
            "reason": verdict.reason,
        }

    # Even a valid signature can be replayed; require its signed ticket and kind.
    # The latest-record path derives kind from the manifest.
    expected_kind = kind if kind is not None else attestation_kind(manifest, record)
    binding_error = _opcert_subject_binding_error(envelope, bound, ticket_id, expected_kind)
    if binding_error is not None:
        return {**base, **binding_error}

    return {
        **base,
        "verified": verdict.verified,
        "verdict": verdict.verdict,
        "reason": verdict.reason,
    }
