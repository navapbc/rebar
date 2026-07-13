"""Operation-certificate attestation kind (``rebar.opcert.v1``) on the SSHSIG substrate.

Story 368c (epic sonic-columned-sturgeon): an *environment* signs a plan-review /
completion-verifier operation certificate with its own asymmetric Ed25519 key via the foundation
substrate (DSSE-PAE envelope + ``sshsig`` scheme + per-kind policy table), and anyone verifies it
against that environment's **out-of-band-pinned** public key. Orthogonal to authorship
(``rebar.authorship.v1``) in actor/threat-model — different key, trust-root, and namespace — but it
**reuses** the substrate and the identity epic's commit-ancestry era-validity *rule*.

Distinct from authorship in two ways the design turns on:

* **Out-of-band keys, explicit SHAs.** Environment keys live in a review-gated config
  (``.rebar/trusted_environments.yaml``), NOT on the auto-pushed tickets branch, so the identity
  epic's tickets-branch resolver (``authorship.resolve_event_commit`` / ``_keyring_for``) cannot be
  reused. Each key record carries explicit ``added_at_commit``/``revoked_at_commit`` git SHAs on
  ``main``; validity is the same ``git merge-base --is-ancestor`` *rule* against the cert's bound
  merged-log commit.
* **Subject binds {ticket id, material fingerprint, merged-log commit}** in an in-toto v1 Statement,
  so a cert cannot be replayed onto a different ticket or a mutated material fingerprint.

**Rollout (keystone e4df).** Op-cert ``SIGNATURE`` events (produced by
``signing.sign_opcert_manifest``, stored per the e4df keystone and read here by
``opcert_from_record``) carry a DSSE ``envelope`` and NO HMAC ``signature``. A pre-upgrade clone
preserves such an event append-only but reads it as **UNSIGNED** — it has no asymmetric verifier —
so op-cert verification requires the upgraded binary: **upgrade verify/reconcile hosts first** (as
with the SSHSIG-authorship rollout and ``TAG_DELTA``). The record-schema extension itself is
additive (legacy HMAC records are byte-unchanged and still HMAC-verify).
"""

from __future__ import annotations

import hashlib
import subprocess

from rebar.attest import authorship, dsse, registry, sshsig

OPCERT_KIND = "rebar.opcert.v1"
OPCERT_NAMESPACE = "rebar.opcert.v1"
PAYLOAD_TYPE = "application/vnd.rebar.opcert.v1+json"


def register_opcert_policy() -> None:
    """Pin ``rebar.opcert.v1`` → the ``sshsig`` scheme in ``registry.POLICY`` (idempotent)."""
    registry.POLICY[OPCERT_KIND] = registry.Policy(
        scheme="sshsig",
        namespace=OPCERT_NAMESPACE,
    )


def opcert_subject_digest(ticket_id: str, material_fingerprint: str, merged_log_commit: str) -> str:
    """The SHA-256 (lowercase hex) digest that an op-cert's subject binds.

    Hashed over the repo's canonical (sorted-key, compact) JSON bytes of
    ``{ticket_id, material_fingerprint, merged_log_commit}``, so signer and verifier
    derive byte-identical bytes independent of dict order. This is the cryptographic
    binding: any change to the ticket id, material fingerprint, or merged-log commit
    changes the digest, so a cert cannot be replayed onto a different ticket or a
    mutated material fingerprint.
    """
    from rebar._store.canonical import canonical_str

    payload = canonical_str(
        {
            "ticket_id": ticket_id,
            "material_fingerprint": material_fingerprint,
            "merged_log_commit": merged_log_commit,
        }
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_opcert_statement(
    ticket_id: str, material_fingerprint: str, merged_log_commit: str
) -> dict:
    """The in-toto Statement an op-cert signature wraps.

    The single subject binds ``ticket_id`` (as ``name``) to the digest over
    ``{ticket_id, material_fingerprint, merged_log_commit}``; the three fields are also
    recorded in ``predicate`` for transparency (the security-relevant binding is the
    hashed digest in ``subject``).
    """
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [
            {
                "name": ticket_id,
                "digest": {
                    "sha256": opcert_subject_digest(
                        ticket_id, material_fingerprint, merged_log_commit
                    )
                },
            }
        ],
        "predicateType": PAYLOAD_TYPE,
        "predicate": {
            "ticket_id": ticket_id,
            "material_fingerprint": material_fingerprint,
            "merged_log_commit": merged_log_commit,
        },
    }


def sign_opcert(
    ticket_id: str,
    material_fingerprint: str,
    merged_log_commit: str,
    *,
    key_path: str,
    principal: str,
) -> dsse.Envelope:
    """Sign an op-cert whose in-toto subject binds ``{ticket_id, material_fingerprint,
    merged_log_commit}``, with the environment's Ed25519 key at ``key_path`` under ``principal``
    (the ``env_id``), in the ``rebar.opcert.v1`` namespace.

    ``ssh-keygen`` availability is asserted first so signing is honest — a missing/too-old
    ssh-keygen raises :class:`sshsig.SshKeygenUnavailable` rather than producing an
    unverifiable envelope. The Statement is serialized to canonical JSON (the DSSE payload)
    and the signature is taken over the DSSE-PAE bytes of ``(PAYLOAD_TYPE, payload)`` under
    the pinned op-cert namespace; ``principal`` (the environment id) becomes the signature
    ``keyid`` (the SSHSIG principal the verifier binds against).
    """
    from rebar._store.canonical import canonical_str

    sshsig.ensure_available()
    statement = build_opcert_statement(ticket_id, material_fingerprint, merged_log_commit)
    payload = canonical_str(statement).encode("utf-8")
    pae = dsse.pae(PAYLOAD_TYPE, payload)
    sig = sshsig.sign(pae, key_path, OPCERT_NAMESPACE)
    return dsse.Envelope(
        PAYLOAD_TYPE,
        payload,
        [dsse.Signature(keyid=principal, sig=sig)],
    )


def verify_opcert(
    envelope: dsse.Envelope,
    ticket_id: str,
    material_fingerprint: str,
    merged_log_commit: str,
    keyring: list[dict],
    *,
    principal: str,
    repo_root: str | None = None,
) -> registry.Verdict:
    """Verify ``envelope`` as an op-cert for ``principal`` bound to
    ``{ticket_id, material_fingerprint, merged_log_commit}`` against ``keyring``
    (records of ``{public_key, added_at_commit, revoked_at_commit}``).

    Two-phase (mirrors ``verify_authorship``'s gate-layer reclassification), all verdict strings
    drawn from the canonical ``verify_signature_result.schema.json`` enum:

    * subject binding mismatch (replay / mutated material) → ``mismatch``;
    * signature not by ANY historical keyring key → ``mismatch``;
    * signature by a real key but that key is not valid at ``merged_log_commit`` →
      ``key_not_valid_at_era``;
    * signature by a key valid at that era → ``certified``.

    Any parse/shape problem, or any git/subprocess/lookup failure, yields a non-verified
    ``Verdict`` — this function never raises (fail-closed).
    """
    import json

    # ── Phase 0: subject binding check (recompute from CALLER-provided fields). ──
    # A replay onto a different ticket, or a mutated material fingerprint, changes the
    # expected digest and is caught here before any signature work.
    expected_digest = opcert_subject_digest(ticket_id, material_fingerprint, merged_log_commit)
    try:
        statement = json.loads(envelope.payload.decode("utf-8"))
        subject = statement["subject"]
        if not isinstance(subject, list) or not subject:
            raise ValueError("empty or non-list subject")
        subject_name = subject[0]["name"]
        subject_hash = subject[0]["digest"]["sha256"]
    except Exception:  # noqa: BLE001 — malformed / non-Statement payload → mismatch, never raise
        return registry.Verdict(
            verified=False,
            verdict="mismatch",
            reason="envelope payload is not a valid in-toto op-cert Statement",
        )
    if subject_name != ticket_id or subject_hash != expected_digest:
        return registry.Verdict(
            verified=False,
            verdict="mismatch",
            reason=(
                "Statement subject does not bind this ticket id and "
                "{ticket_id, material_fingerprint, merged_log_commit} digest"
            ),
        )

    # ── Phase 1: is the signature by ANY historical keyring key at all? ──
    all_keys: list[str] = [
        rec["public_key"]
        for rec in keyring
        if isinstance(rec, dict) and isinstance(rec.get("public_key"), str) and rec["public_key"]
    ]
    any_key_root = authorship.allowed_signers_from_keys(all_keys, principal)
    any_verdict = registry.verify(OPCERT_KIND, envelope, any_key_root)
    if not any_verdict.verified:
        # Not signed by any key the environment has ever held → a forgery / foreign key.
        return registry.Verdict(
            verified=False,
            verdict="mismatch",
            reason=(f"signature is not by any key in the keyring for principal {principal!r}"),
        )

    # ── Phase 2: is the signing key valid AT ``merged_log_commit``? ──
    # A key is valid iff its add-commit is an ancestor of merged_log_commit AND
    # (it is not revoked, or its revoke-commit is NOT an ancestor of merged_log_commit).
    try:
        git_dir = repo_root or "."

        def _is_ancestor(ancestor: str, descendant: str) -> bool:
            proc = subprocess.run(
                ["git", "-C", git_dir, "merge-base", "--is-ancestor", ancestor, descendant],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return proc.returncode == 0

        valid_keys: list[str] = []
        for rec in keyring:
            if not isinstance(rec, dict):
                continue
            pub = rec.get("public_key")
            added_at = rec.get("added_at_commit")
            revoked_at = rec.get("revoked_at_commit")
            if not isinstance(pub, str) or not pub:
                continue
            if not isinstance(added_at, str) or not added_at:
                continue
            if not _is_ancestor(added_at, merged_log_commit):
                continue
            if (
                isinstance(revoked_at, str)
                and revoked_at
                and _is_ancestor(revoked_at, merged_log_commit)
            ):
                continue
            valid_keys.append(pub)
    except Exception:  # noqa: BLE001 — ANY git/lookup failure → non-verified, never raise (fail-closed)
        return registry.Verdict(
            verified=False,
            verdict="key_not_valid_at_era",
            reason=(
                f"op-cert era verification for principal {principal!r} failed (git/lookup error)"
            ),
        )

    if valid_keys:
        era_root = authorship.allowed_signers_from_keys(valid_keys, principal)
        era_verdict = registry.verify(OPCERT_KIND, envelope, era_root)
        if era_verdict.verified:
            return registry.Verdict(
                verified=True,
                verdict="certified",
                reason=(
                    f"op-cert signed by a key of {principal!r} valid at "
                    f"commit {merged_log_commit!r}"
                ),
            )

    # A real key of this environment signed it, but that key was not valid at the era.
    return registry.Verdict(
        verified=False,
        verdict="key_not_valid_at_era",
        reason=(
            f"signing key of {principal!r} is real but not valid at "
            f"commit {merged_log_commit!r} (not yet added, or already revoked)"
        ),
    )


def opcert_from_record(record: dict) -> tuple[dsse.Envelope, dict] | None:
    """Reconstruct an op-cert from a stored ``attestations[kind]`` record (keystone e4df).

    Returns ``(envelope, {"material_fingerprint": …, "merged_log_commit": …})`` when the record
    carries an op-cert (an encoded DSSE ``envelope`` field), or ``None`` for a legacy HMAC record
    (no envelope). The returned envelope is fed to :func:`verify_opcert` with a pinned keyring.

    Never raises on a malformed envelope — a decode failure yields ``None`` (fail-closed).
    """
    encoded = record.get("envelope")
    if not isinstance(encoded, str) or not encoded:
        return None
    try:
        envelope = dsse.decode(encoded)
    except Exception:  # noqa: BLE001 — malformed envelope → None, never raise (fail-closed)
        return None
    return (
        envelope,
        {
            "material_fingerprint": record.get("material_fingerprint"),
            "merged_log_commit": record.get("merged_log_commit"),
        },
    )
