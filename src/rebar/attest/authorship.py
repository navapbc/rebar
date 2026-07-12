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

import json
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


def keyop_payload(op: str, identity_id: str, public_key: str) -> bytes:
    """The canonical bytes a KEY-op signature covers (epic gnu-whale-ichor / e165).

    ``op`` is ``"KEY_ADD"`` / ``"KEY_REVOKE"``. The payload binds the operation to a
    specific identity AND public key, so a signature over one key-op can never be
    replayed as authorization for a different op / identity / key. Canonical (sorted-key,
    compact) so signer and verifier derive byte-identical bytes independent of dict order.
    """
    from rebar._store.canonical import canonical_str

    return canonical_str({"op": op, "identity_id": identity_id, "public_key": public_key}).encode(
        "utf-8"
    )


def _keyring_for(identity_id: str, *, repo_root=None) -> list:
    """The identity's epoch-scoped ``keyring`` records, or ``[]`` on ANY lookup problem
    (unknown id, corrupt store, non-identity, I/O). Never raises — mirrors
    :func:`resolve_trust_root`'s fail-closed contract."""
    import rebar

    try:
        ticket = rebar.show_ticket(identity_id, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — any lookup failure → no keyring, never raise
        return []
    if not isinstance(ticket, dict):
        return []
    ring = ticket.get("keyring")
    return ring if isinstance(ring, list) else []


def verify_authorship_at_epoch(
    envelope: dsse.Envelope, identity_id: str, epoch: int, *, repo_root=None
) -> registry.Verdict:
    """Verify ``envelope`` against ONLY the keys the identity's keyring holds as valid
    at ``epoch`` (epic gnu-whale-ichor / e165).

    Unlike :func:`verify_authorship` (which trusts the identity's CURRENTLY-valid keys),
    the trust root here is built from exactly the keyring records whose
    ``[added_epoch, revoked_epoch)`` window contains ``epoch`` — so a signature made by a
    key that had not yet been added, or was already revoked, at ``epoch`` does NOT verify.
    Any lookup problem or an empty epoch-scoped trust root yields a non-verified
    ``"unknown_principal"`` Verdict; this function never raises for a lookup problem.
    """
    from rebar.reducer._processors import keys_valid_at_epoch

    keys = keys_valid_at_epoch(_keyring_for(identity_id, repo_root=repo_root), epoch)
    if not keys:
        return registry.Verdict(
            verified=False,
            verdict="unknown_principal",
            reason=(
                f"no keys valid at epoch {epoch} for identity {identity_id!r} "
                "(key not yet added, already revoked, or identity unknown/keyless)"
            ),
        )
    trust_root = allowed_signers_from_keys(keys, principal=identity_id)
    return registry.verify(AUTHORSHIP_KIND, envelope, trust_root)


def epoch_for_position(identity_id: str, event_position: str, *, repo_root=None) -> int:
    """The keyring epoch in effect at the point event ``event_position`` was written
    (epic gnu-whale-ichor / e165). SNAPSHOT-AWARE.

    ``event_position`` is a canonical ``{HLC-timestamp}-{uuid}`` event filename prefix.
    The epoch is the identity's ``keyring_epoch`` cursor value as of that position: it
    starts from the value restored by the identity's latest SNAPSHOT (whose compacted
    KEY-event files are retired and must NOT be re-scanned) and adds one for each
    post-snapshot KEY event whose filename sorts strictly BEFORE ``event_position``. Any
    lookup problem degrades to ``0`` (never raises)."""
    import os

    from rebar._commands._seam import tracker_dir
    from rebar.reducer._cache import RETIRED_SUFFIX, is_active_event
    from rebar.reducer._replay import scan_for_latest_snapshot
    from rebar.reducer._sort import event_sort_key

    try:
        ticket_dir = os.path.join(str(tracker_dir(repo_root)), identity_id)
        names = [
            f
            for f in os.listdir(ticket_dir)
            if not f.startswith(".") and f.endswith(".json") and is_active_event(f)
        ]
    except OSError:
        return 0
    event_files = sorted((os.path.join(ticket_dir, f) for f in names), key=event_sort_key)

    # Pass 1: find the latest SNAPSHOT and read the keyring_epoch it froze into
    # compiled_state — the epoch base (compacted KEY events before it are retired).
    snap_idx, _uuids = scan_for_latest_snapshot(event_files)
    base_epoch = 0
    if snap_idx is not None:
        try:
            with open(event_files[snap_idx], encoding="utf-8") as f:
                compiled = json.load(f).get("data", {}).get("compiled_state", {})
            base_epoch = int(compiled.get("keyring_epoch") or 0)
        except (OSError, ValueError, TypeError):
            base_epoch = 0

    # Pass 2: each post-snapshot KEY event that precedes event_position consumed one epoch.
    start = snap_idx if snap_idx is not None else 0
    steps = 0
    for path in event_files[start:]:
        base = os.path.basename(path).removesuffix(RETIRED_SUFFIX)
        # The comparable position prefix is the leading "{ts}-{uuid}" (drop "-TYPE.json").
        try:
            with open(path, encoding="utf-8") as f:
                etype = json.load(f).get("event_type", "")
        except (OSError, ValueError):
            continue
        if etype not in ("KEY_ADD", "KEY_REVOKE"):
            continue
        if _position_prefix(base) < event_position:
            steps += 1
    return base_epoch + steps


def _position_prefix(filename: str) -> str:
    """The ``{HLC-timestamp}-{uuid}`` position prefix of an event filename (drop the
    trailing ``-{TYPE}.json``). Used to compare a KEY event's position against a target
    ``event_position`` without the event-type suffix skewing the lexical compare."""
    stem = filename.removesuffix(".json")
    # Strip the trailing "-TYPE" segment (event types are uppercase, no internal '-').
    idx = stem.rfind("-")
    return stem[:idx] if idx != -1 else stem


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
