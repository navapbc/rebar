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


def _signing_key_sha256(public_key: str | None) -> str | None:
    """``SHA256:`` fingerprint of an OpenSSH public-key line, as ``ssh-keygen -l`` prints it."""
    import base64
    import binascii
    import hashlib

    parts = public_key.split() if public_key else []
    if len(parts) < 2:
        return None
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError):
        return None
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def _embedded_signer_key(envelope) -> str | None:
    """The public key carried inside the envelope's SSHSIG, or ``None`` when unreadable."""
    from rebar.attest import sshsig

    sigs = list(envelope.signatures) if envelope is not None else []
    return sshsig.embedded_public_key(sigs[0].sig) if sigs else None


def trusted_opcert_pairs(repo_root) -> frozenset[tuple[str, str]] | None:
    """The ``(env_id, "<key-type> <base64>")`` pairs in ``verify.trusted_opcert_keys``.

    ``None`` means no list is configured (validity decides). A configuration that cannot be
    read raises :class:`rebar.config.ConfigError`; callers fail closed rather than reading
    a broken configuration as "no list" (ADR 0125)."""
    return opcert_pairs(config.compose_config(root=repo_root).verify.trusted_opcert_keys)


def opcert_pairs(entries) -> frozenset[tuple[str, str]] | None:
    """Parse ``verify.trusted_opcert_keys`` entries into ``(env_id, "<key-type> <base64>")``
    pairs; ``None`` for an empty list (validity decides)."""
    if not entries:
        return None
    return frozenset((p[0], f"{p[1]} {p[2]}") for p in (e.split() for e in entries))


def _trust_list_verdict(base: dict, principal: str, signer_key: str | None, repo_root) -> dict:
    """Judge an already-valid, correctly bound certificate against the trust list.

    Returns the final result: ``certified`` with trust basis ``trust_list`` when the
    ``(principal, key)`` pair is listed, otherwise ``untrusted``. An unreadable list fails
    closed as ``untrusted``."""
    from rebar._opcert_signing import _ssh_pub_body

    fingerprint = base.get("signing_key")
    try:
        pairs = trusted_opcert_pairs(repo_root) or frozenset()
    except Exception as exc:  # noqa: BLE001 — an unreadable trust list never certifies
        return {
            **base,
            "verified": False,
            "verdict": "untrusted",
            "reason": f"verify.trusted_opcert_keys could not be read ({exc})",
        }
    if (principal, _ssh_pub_body(signer_key)) in pairs:
        return {
            **base,
            "trust_basis": "trust_list",
            "verified": True,
            "verdict": "certified",
            "reason": f"signed by {fingerprint}, listed for environment {principal!r}",
        }
    return {
        **base,
        "verified": False,
        "verdict": "untrusted",
        "reason": (
            f"op-cert is validly signed by key {fingerprint} for environment {principal!r}, "
            "but that pair is not in verify.trusted_opcert_keys"
        ),
    }


def _trust_list_configured(repo_root) -> bool:
    """True when a trust list is set, or when the configuration cannot be read (fail closed)."""
    try:
        return trusted_opcert_pairs(repo_root) is not None
    except Exception:  # noqa: BLE001 — unreadable config is judged by _trust_list_verdict
        return True


def verify_opcert_record(
    record: dict, ticket_id: str, *, kind: str | None = None, repo_root=None
) -> dict:
    """Verify a DSSE op-cert record: validity by default, a trust list when configured.

    The signature must verify under the key embedded in it, for the record's principal, and
    its signed subject must bind this ticket and kind (ADR 0125). With no
    ``verify.trusted_opcert_keys`` list that is the whole check (trust basis
    ``envelope_key``); with one, the (environment, key) pair must also be listed, else the
    verdict is ``untrusted``. ``verify.require_environment`` requires a list.
    Return authenticated material, commit, and manifest for freshness checks;
    verification never creates a key.
    """
    from rebar.attest import authorship, opcert, registry
    from rebar.attest.opcert import OPCERT_KIND
    from rebar.reducer._processors import attestation_kind
    from rebar.signing import rebar_version_from_manifest, verified_at_sha_from_manifest

    raw_manifest = record.get("manifest")
    manifest = raw_manifest if isinstance(raw_manifest, list) else []

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
        # ``envelope_key`` (validity only) or ``trust_list`` (listed pair).
        "trust_basis": None,
        # Which key signed (its SHA256 fingerprint), whatever the verdict (ADR 0125).
        "signing_key": _signing_key_sha256(_embedded_signer_key(envelope)),
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

    with_list = _trust_list_configured(repo_root)
    if required_env is not None and not with_list:
        # A principal is the certificate's own claim; only a trust list can vouch for it.
        return {
            **base,
            "verified": False,
            "verdict": "foreign_key",
            "reason": (
                f"verify.require_environment is set to {required_env!r}, which requires a "
                "verify.trusted_opcert_keys list naming that environment's keys (ADR 0125)"
            ),
        }

    # Validity is judged under the key the signature itself carries (ADR 0125): the signature,
    # namespace and principal binding are all still checked in full.
    signer_key = _embedded_signer_key(envelope)
    base = {**base, "trust_basis": "envelope_key" if signer_key else None}
    if not signer_key:
        return {
            **base,
            "verified": False,
            "verdict": "mismatch",
            "reason": (
                f"the signature of {principal!r} carries no readable public key, so it cannot "
                "verify (corrupted or not an SSHSIG signature)"
            ),
        }

    trust_root = authorship.allowed_signers_from_keys([signer_key], principal)
    verdict = registry.verify(OPCERT_KIND, envelope, trust_root)
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
    if with_list:
        return _trust_list_verdict(base, principal, signer_key, repo_root)

    return {
        **base,
        "verified": verdict.verified,
        "verdict": verdict.verdict,
        "reason": verdict.reason,
    }


def verify_completion_opcert(
    envelope,
    ticket_id: str,
    material_fingerprint: str,
    merged_log_commit: str,
    *,
    kind: str,
    required_env: str,
    trusted_pairs: frozenset[tuple[str, str]],
):
    """Merge-gate check of one op-cert against the gating environment's trust list (ADR 0125).

    The certificate must name ``required_env``, be signed by a key listed for that environment,
    verify under that key, and bind ``{ticket_id, material_fingerprint, merged_log_commit,
    kind}``. Returns a :class:`rebar.attest.registry.Verdict`; never raises."""
    from rebar._opcert_signing import _ssh_pub_body
    from rebar.attest import authorship, opcert, registry

    sigs = list(envelope.signatures)
    principal = sigs[0].keyid if sigs else None
    if principal != required_env:
        return registry.Verdict(
            verified=False,
            verdict="foreign_key",
            reason=f"op-cert names environment {principal!r}, not {required_env!r}",
        )
    signer_key = _embedded_signer_key(envelope)
    if signer_key is None or (principal, _ssh_pub_body(signer_key)) not in trusted_pairs:
        return registry.Verdict(
            verified=False,
            verdict="untrusted",
            reason=(
                f"signing key {_signing_key_sha256(signer_key)} is not listed for "
                f"{principal!r} in verify.trusted_opcert_keys"
            ),
        )
    expected = opcert.opcert_subject_digest(
        ticket_id, material_fingerprint, merged_log_commit, kind
    )
    try:
        subject = json.loads(envelope.payload)["subject"][0]
        bound = subject["name"] == ticket_id and subject["digest"]["sha256"] == expected
    except Exception:  # noqa: BLE001 — a malformed Statement binds nothing
        bound = False
    if not bound:
        return registry.Verdict(
            verified=False,
            verdict="mismatch",
            reason="op-cert subject does not bind this ticket, material and merged-log commit",
        )
    root = authorship.allowed_signers_from_keys([signer_key], principal)
    return registry.verify(opcert.OPCERT_KIND, envelope, root)
