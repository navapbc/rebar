"""Same-environment op-cert verify path, split from ``_opcert_signing`` (module-size cap).

This module owns the ``rebar.opcert.v1`` DSSE verify machinery: the subject-binding check,
the environment-pinning resolution (``verify.require_environment`` +
``verify.opcert_enforce_since``), the trust-root assembly, and the top-level
:func:`verify_opcert_record`. It is carved out of ``_opcert_signing`` purely to keep both units
under the module-size cap (docs/architecture.md, ``.github/module-size-limit.txt``); the split is
along the existing mint/verify call-graph seam (mint never calls into verify). It imports the two
custody helpers it needs (:func:`opcert_principal`, :func:`_opcert_own_public_keys`) from
``_opcert_signing`` LAZILY inside the functions that use them -- matching this module family's
existing lazy-import style -- so neither import direction forms a cycle regardless of which module
is loaded first (``_opcert_signing`` re-exports :func:`verify_opcert_record` at end-of-file).
"""

from __future__ import annotations

import json

from rebar import config


def _opcert_subject_binding_error(
    envelope, bound: dict, ticket_id: str, expected_kind
) -> dict | None:
    """Return a ``mismatch`` result-fragment if the op-cert's SIGNED subject does not bind
    ``ticket_id`` + ``expected_kind`` (finding A), else ``None`` (binding holds).

    ``bound`` is the SIGNED in-toto predicate (from :func:`opcert.opcert_from_record`); the caller
    has already verified the DSSE signature over the payload, so ``bound`` and the envelope's
    subject digest are authenticated. This mirrors :func:`opcert.verify_opcert`'s Phase-0 subject
    check for the same-environment local path (which has NO caller-supplied expected material /
    commit — the merge-gate's era/keyring recompute is deliberately NOT reproduced here). It
    confirms the authenticated subject actually names the ticket + attestation-kind slot being
    verified, defeating a cross-ticket / cross-kind replay of an otherwise-valid cert:

      * a valid cert the environment signed for ticket X, copied onto ticket Y's record →
        ``bound ticket_id`` (X) != ``ticket_id`` (Y) → ``mismatch``;
      * a valid cert signed for kind K1, filed under kind K2's slot → ``bound kind`` (K1) !=
        ``expected_kind`` (K2) → ``mismatch``.

    Also recomputes the subject digest from the signed predicate and requires it to equal the
    envelope's own signed subject digest (a consistency check: a predicate that disagrees with the
    signed subject is rejected)."""
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
    """``verify.require_environment`` — the environment an operator has REQUIRED op-certs to come
    from — or ``None`` when unset (the default, and this project's posture).

    Unset is the operator-ruled current policy: environment identity is not a gate (bug c21f).
    Setting it re-enables the trusted-set restriction, which is a deferred FUTURE feature; the
    runbook (``infra/runbooks/mcp-opcert-enforcement-flip.md``) sets it together with
    ``verify.opcert_enforce_since``, the merge gate's grandfather boundary — a MERGED-LOG anchor
    that has no meaning on this local, per-record path, so only the environment key is read here.
    Fails OPEN on an unreadable config: an absent/broken ``rebar.toml`` must not silently turn a
    restriction ON that the operator never configured."""
    try:
        return config.compose_config(root=repo_root).verify.require_environment or None
    except Exception:  # noqa: BLE001 — no readable config ⇒ no required environment
        return None


def _pinned_environment_keys(principal: str, repo_root) -> list[str]:
    """Public keys pinned for ``principal`` in ``.rebar/trusted_environments.yaml`` (the
    out-of-band, review-gated trust root the ``verify-opcert`` merge gate uses). Empty when the
    environment is not pinned, or the config is absent/malformed."""
    try:
        from rebar.attest import trusted_env

        keyring = trusted_env.trusted_env_keyring(principal, repo_root) or []
    except Exception:  # noqa: BLE001 — absent/malformed pin config ⇒ nothing pinned here
        return []
    return [
        rec["public_key"]
        for rec in keyring
        if isinstance(rec, dict) and isinstance(rec.get("public_key"), str) and rec["public_key"]
    ]


def _opcert_trust_root(
    principal: str, own_principal: str, tracker: str, envelope, repo_root, required_env: str | None
) -> tuple[list[str], str | None]:
    """The keys to verify ``envelope`` under, plus the ``trust_basis`` label naming their source.

    Strongest first: this environment's OWN key when it is the signer; then the key PINNED
    out-of-band for that environment; then — only while the trusted-set restriction is OFF — the
    signer's own key as carried inside the SSHSIG blob. That last basis is self-consistent rather
    than pinned: ``ssh-keygen -Y verify`` still checks the signature, namespace and principal
    binding in full, so a forged or altered envelope is still rejected; what it does not establish
    is that the key belongs to a KNOWN environment. Under the operator's current policy that is
    precisely the property that is not gated, and the label makes the weaker basis visible.

    With ``verify.require_environment`` SET the envelope-key fallback is withheld: enforcement must
    bind to the PINNED key, never to a key the certificate supplies about itself."""
    from rebar._opcert_signing import _opcert_own_public_keys

    if principal == own_principal:
        own_pubs = _opcert_own_public_keys(tracker)
        if own_pubs:
            return own_pubs, "own_key"
    pinned = _pinned_environment_keys(principal, repo_root)
    if pinned:
        return pinned, "pinned_environment"
    if required_env is not None:
        return [], None
    from rebar.attest import sshsig

    sigs = list(envelope.signatures)
    embedded = sshsig.embedded_public_key(sigs[0].sig) if sigs else None
    return ([embedded], "envelope_key") if embedded else ([], None)


def verify_opcert_record(
    record: dict, ticket_id: str, *, kind: str | None = None, repo_root=None
) -> dict:
    """Verify an ``envelope``-bearing op-cert record. The SIGNATURE is the gate; the signing
    ENVIRONMENT is not (bug c21f).

    Operator policy, verbatim: *"Certification environment should not currently be a gate. Any
    certification is as good as any other certification right now. Limited to a trusted set of
    environments is a future feature, but not currently in use."* So a cert minted by the on-box
    MCP server certifies for a local CLI worktree and vice versa — identity alone never refuses.
    This mirrors the same call already made for the bugfix-size gate (bug 846b): gate on the FACT
    of certification, never on its SOURCE.

    Translation table (the wrapper's contract; downstream readers see the uniform verify shape):
      * ``verify.require_environment`` SET (the opt-in future feature, unset by default) and the
        cert's principal is not that environment → ``foreign_key``, without invoking the scheme;
      * otherwise the scheme runs — ``registry.verify(OPCERT_KIND, envelope, trust_root)`` against
        the strongest available trust root (see :func:`_opcert_trust_root`), THEN the SIGNED
        subject-binding check (:func:`_opcert_subject_binding_error`), mapping
        ``verified → certified`` and passing the scheme ``verdict``/``reason`` through
        (``certified`` / ``mismatch`` / ``invalid`` / ``unavailable`` / ``unknown_kind`` /
        ``unknown_scheme``). ``mismatch`` and ``unsigned`` keep their exact meaning: not gating on
        environment is NOT gating on nothing, and an altered/forged envelope is still refused.

    ``trust_basis`` names WHICH key certified — ``own_key`` / ``pinned_environment`` /
    ``envelope_key`` — so a weaker basis is VISIBLE rather than silently folded into ``certified``.

    SECURITY (findings A + B):
      * ``kind`` is the attestation-kind SLOT being verified (threaded from ``verify_signature`` /
        ``verify_attestations``; ``None`` for the legacy most-recent path, which falls back to the
        manifest-derived kind — the slot key the reducer would file the record under). The SIGNED
        subject must bind ``ticket_id`` + this kind, or the cert is a replay and is rejected
        (``mismatch``) — the signature verifying is necessary but NOT sufficient.
      * the bound ``{material_fingerprint, merged_log_commit, manifest}`` surfaced into the result
        (the last as ``signed_manifest``) are sourced from the SIGNED payload (the in-toto predicate
        via ``opcert.opcert_from_record``), NEVER the attacker-writable plaintext record mirror, so
        ``compute_validity`` compares freshness (stale-code / stale-regver) and material against
        authenticated values (verify-then-extract).

    ``key_id`` carries the record's ``principal`` (keeping ``drift_refresh_candidate``'s provenance
    read working). Never CREATES a key — verification is read-only."""
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
        # SECURITY (finding B): the AUTHENTICATED material fingerprint + bound code commit, sourced
        # from the SIGNED payload (never the plaintext record mirror). compute_validity reads THESE
        # for op-cert (algorithm="sshsig") records so a mutated plaintext mirror cannot flip a
        # freshness/material verdict. Absent (None) for a malformed/undecodable envelope.
        "material_fingerprint": bound.get("material_fingerprint"),
        "merged_log_commit": bound.get("merged_log_commit"),
        # SECURITY (stale-code / stale-regver findings): the AUTHENTICATED manifest, sourced from
        # the SIGNED payload (the in-toto predicate's ``manifest``), NEVER the plaintext record
        # mirror. compute_validity's plan-review branch reads THIS (via _authoritative_manifest) for
        # manifest_deps (stale-code), manifest_regver (stale-regver), and the pinned-SHA basis, so a
        # mutated plaintext manifest cannot flip a freshness verdict. ``None`` for a legacy op-cert
        # minted before the manifest was bound; the reader then falls back to the plaintext manifest
        # (which is that record's only manifest — no weakening vs. today's behaviour).
        "signed_manifest": bound.get("manifest"),
        # Unspoofable op-cert marker: set by THIS code path, which is selected on the
        # ``record.envelope`` presence — NOT the attacker-writable ``algorithm`` field. Keyed on
        # this (not ``algorithm``) so an attacker cannot force the plaintext-manifest material path
        # by mutating ``algorithm`` while keeping the envelope.
        "opcert": True,
        # Which key certified this cert (bug c21f). ``None`` until a trust root is chosen; then
        # ``own_key`` (this environment's) / ``pinned_environment`` (a key pinned out-of-band in
        # ``.rebar/trusted_environments.yaml``) / ``envelope_key`` (the signer's own key, carried
        # inside the SSHSIG blob — self-consistent, but not tied to a KNOWN environment). Surfaced
        # so the weakest basis is visible to readers and audits, never silent.
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
    if not verdict.verified:
        return {
            **base,
            "verified": verdict.verified,
            "verdict": verdict.verdict,
            "reason": verdict.reason,
        }

    # SECURITY (finding A): the signature verifies — but a valid cert can be a REPLAY. Enforce the
    # SIGNED subject binding: the cert must bind THIS ticket + THIS attestation-kind slot. The slot
    # is the threaded `kind`; for the legacy most-recent path (`kind is None`) fall back to the
    # manifest-derived kind (the key the reducer files the record under).
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
