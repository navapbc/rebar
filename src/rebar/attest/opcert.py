"""Operation-certificate attestation kind (``rebar.opcert.v1``) on the SSHSIG substrate.

Story 368c (epic sonic-columned-sturgeon): an *environment* signs a plan-review /
completion-verifier operation certificate with its own asymmetric Ed25519 key via the foundation
substrate (DSSE-PAE envelope + ``sshsig`` scheme + per-kind policy table), and anyone verifies it
against that environment's **out-of-band-pinned** public key. Orthogonal to authorship
(``rebar.authorship.v1``) in actor/threat-model — different key, trust-root, and namespace — but it
**reuses** the substrate and the identity epic's commit-ancestry era-validity *rule*.

Distinct from authorship in two ways the design turns on:

* **Out-of-band keys, era at the STORAGE ANCHOR (story 4214 — Option B).** Environment keys live
  in a review-gated config (``.rebar/trusted_environments.yaml``), NOT on the auto-pushed tickets
  branch. Each key record carries explicit ``added_at_log_position``/``revoked_at_log_position``
  TICKETS-BRANCH log positions (``{timestamp}-{uuid}`` event-position strings; the revoke may be
  ``null``). A key's era-validity is evaluated at the certificate's STORAGE ANCHOR ``S`` — the
  tickets-branch commit that introduced the terminal envelope-bearing ``SIGNATURE`` event — NOT at
  the cert's SELF-CHOSEN ``merged_log_commit`` (which a revoked-key holder could backdate to a
  pre-revocation ancestor to make a stale key "verify"). Validity reuses the identity epic's shared
  ancestry + intra-commit-position rule (``authorship.keys_valid_at_anchor`` +
  ``authorship.resolve_position_commit``): the era boundary positions resolve to their introducing
  tickets-branch commits and are compared against ``S``. ``merged_log_commit`` remains a signed
  subject field (so signatures still verify) but carries NO key-validity semantics.
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


def opcert_subject_digest(
    ticket_id: str, material_fingerprint: str, merged_log_commit: str, kind: str
) -> str:
    """The SHA-256 (lowercase hex) digest that an op-cert's subject binds.

    Hashed over the repo's canonical (sorted-key, compact) JSON bytes of
    ``{ticket_id, material_fingerprint, merged_log_commit, kind}``, so signer and verifier
    derive byte-identical bytes independent of dict order. This is the cryptographic
    binding: any change to the ticket id, material fingerprint, merged-log commit, OR the
    attestation ``kind`` changes the digest. Binding ``kind`` closes a kind-confusion gap:
    the ``attestations[kind]`` slot is keyed by the UNSIGNED ``manifest[0]``, so without this
    a cert signed for one kind (e.g. ``plan-review``) could be filed under another
    (``completion-verifier``) and still verify; with ``kind`` in the signed subject, a
    moved cert fails verification.
    """
    from rebar._store.canonical import canonical_str

    payload = canonical_str(
        {
            "ticket_id": ticket_id,
            "material_fingerprint": material_fingerprint,
            "merged_log_commit": merged_log_commit,
            "kind": kind,
        }
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_opcert_statement(
    ticket_id: str, material_fingerprint: str, merged_log_commit: str, kind: str
) -> dict:
    """The in-toto Statement an op-cert signature wraps.

    The single subject binds ``ticket_id`` (as ``name``) to the digest over
    ``{ticket_id, material_fingerprint, merged_log_commit, kind}``; the fields are also
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
                        ticket_id, material_fingerprint, merged_log_commit, kind
                    )
                },
            }
        ],
        "predicateType": PAYLOAD_TYPE,
        "predicate": {
            "ticket_id": ticket_id,
            "material_fingerprint": material_fingerprint,
            "merged_log_commit": merged_log_commit,
            "kind": kind,
        },
    }


def sign_opcert(
    ticket_id: str,
    material_fingerprint: str,
    merged_log_commit: str,
    *,
    kind: str,
    key_path: str,
    principal: str,
) -> dsse.Envelope:
    """Sign an op-cert whose in-toto subject binds ``{ticket_id, material_fingerprint,
    merged_log_commit, kind}``, with the environment's Ed25519 key at ``key_path`` under
    ``principal`` (the ``env_id``), in the ``rebar.opcert.v1`` namespace. Binding ``kind``
    (the attestation kind, e.g. ``completion-verifier``) into the signed subject prevents a
    cert signed for one kind from being filed under and accepted for another.

    ``ssh-keygen`` availability is asserted first so signing is honest — a missing/too-old
    ssh-keygen raises :class:`sshsig.SshKeygenUnavailable` rather than producing an
    unverifiable envelope. The Statement is serialized to canonical JSON (the DSSE payload)
    and the signature is taken over the DSSE-PAE bytes of ``(PAYLOAD_TYPE, payload)`` under
    the pinned op-cert namespace; ``principal`` (the environment id) becomes the signature
    ``keyid`` (the SSHSIG principal the verifier binds against).
    """
    from rebar._store.canonical import canonical_str

    sshsig.ensure_available()
    statement = build_opcert_statement(ticket_id, material_fingerprint, merged_log_commit, kind)
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
    kind: str,
    principal: str,
    storage_anchor_commit: str,
    storage_anchor_position: str | None = None,
    repo_root: str | None = None,
) -> registry.Verdict:
    """Verify ``envelope`` as an op-cert for ``principal`` bound to
    ``{ticket_id, material_fingerprint, merged_log_commit, kind}`` against ``keyring``
    (records of ``{public_key, added_at_log_position, revoked_at_log_position}``). ``kind`` is bound
    so a cert signed for one attestation kind cannot be accepted for another (kind-confusion).

    Era-validity is evaluated at the certificate's STORAGE ANCHOR
    ``(storage_anchor_commit, storage_anchor_position)`` — the tickets-branch commit that introduced
    the terminal envelope-bearing ``SIGNATURE`` event and its intra-commit position — NOT at
    ``merged_log_commit`` (story 4214 / Option B). ``merged_log_commit`` stays in the signed subject
    digest (so the signature still verifies) but no longer anchors key validity; this closes the
    rollback where a revoked-key holder backdates ``merged_log_commit`` to a pre-revocation
    ancestor.

    Two-phase (mirrors ``verify_authorship``'s gate-layer reclassification), all verdict strings
    drawn from the canonical ``verify_signature_result.schema.json`` enum:

    * subject binding mismatch (replay / mutated material) → ``mismatch``;
    * signature not by ANY historical keyring key → ``mismatch``;
    * signature by a real key but that key is not valid at the storage anchor →
      ``key_not_valid_at_era``;
    * signature by a key valid at that anchor → ``certified``.

    The era boundary positions (``added_at_log_position`` / ``revoked_at_log_position``) resolve to
    their introducing tickets-branch commits and are compared against ``storage_anchor_commit`` with
    the SAME ancestry + intra-commit-position rule authorship uses (the SHARED
    ``authorship.keys_valid_at_anchor`` + ``authorship.resolve_position_commit`` — not a duplicate).

    Any parse/shape problem, or any git/subprocess/lookup failure, yields a non-verified
    ``Verdict`` — this function never raises (fail-closed).
    """
    import json

    # ── Phase 0: subject binding check (recompute from CALLER-provided fields). ──
    # A replay onto a different ticket, or a mutated material fingerprint, changes the
    # expected digest and is caught here before any signature work.
    expected_digest = opcert_subject_digest(
        ticket_id, material_fingerprint, merged_log_commit, kind
    )
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

    # ── Phase 2: is the signing key valid AT the STORAGE ANCHOR ``S``? ──
    # A key is valid iff its add-position resolves to an ANCESTOR of S AND (it is not revoked, or
    # its revoke-position does NOT resolve to an ancestor of S). Era boundaries are TICKETS-BRANCH
    # log positions; ancestry is decided on the tracker (tickets branch), NOT the code repo, and
    # anchored on S rather than the cert's self-chosen merged_log_commit (story 4214 / Option B).
    try:
        from rebar._commands._seam import tracker_dir

        tracker = str(tracker_dir(repo_root))

        def _is_ancestor(ancestor: str, descendant: str) -> bool:
            proc = subprocess.run(
                ["git", "-C", tracker, "merge-base", "--is-ancestor", ancestor, descendant],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return proc.returncode == 0

        def _resolve(position: str) -> str | None:
            return authorship.resolve_position_commit(position, tracker, repo_root=repo_root)

        # Normalize the out-of-band keyring records to the shared predicate's {added_at, revoked_at}
        # shape (the config field names are the log-position variants), then apply the SINGLE era
        # rule shared with authorship — never a re-implementation.
        records = [
            {
                "public_key": rec.get("public_key"),
                "added_at": rec.get("added_at_log_position"),
                "revoked_at": rec.get("revoked_at_log_position"),
            }
            for rec in keyring
            if isinstance(rec, dict)
        ]
        valid_keys = authorship.keys_valid_at_anchor(
            records,
            storage_anchor_commit,
            storage_anchor_position,
            resolve=_resolve,
            is_ancestor=_is_ancestor,
        )
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
                    f"storage anchor {storage_anchor_commit!r}"
                ),
            )

    # A real key of this environment signed it, but that key was not valid at the storage anchor.
    return registry.Verdict(
        verified=False,
        verdict="key_not_valid_at_era",
        reason=(
            f"signing key of {principal!r} is real but not valid at "
            f"storage anchor {storage_anchor_commit!r} (not yet added, or already revoked)"
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
