"""Op-cert producer-signing machinery for the ``signing.sign_manifest`` seam (story 8d8e).

The gate producers mint ``rebar.opcert.v1`` DSSE op-certs with the ambient environment's
auto-generated Ed25519 key (``<tracker>/.opcert-key``) instead of the legacy per-clone HMAC secret,
so a local run and a trusted-server run produce the SAME artifact — one signature per verdict, no
double-signing. This module owns the environment-key custody (race-safe genesis + principal
resolution), the mint path, and the same-environment verify path; ``rebar.signing`` keeps the
public seam (``sign_manifest`` / ``verify_attestation_record``) as thin delegators over it.

It is split out of ``signing.py`` purely to keep both units under the module-size soft cap
(docs/architecture.md); it deliberately does NOT import ``rebar.signing`` at module scope (the two
pure manifest helpers it needs are imported lazily inside the verify path) so there is no cycle.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

from rebar import config

logger = logging.getLogger(__name__)

# The environment's op-cert key: a passphrase-free Ed25519 keypair auto-generated on first signing
# use at ``<tracker>/.opcert-key`` (+``.pub``), git-ignored, mirroring the HMAC ``.signing-key``
# genesis it replaces. The producers sign with it; anyone verifies against the env's public key.
OPCERT_KEY_FILE = ".opcert-key"

# The DSSE subject ``kind`` bound into a raw ``rebar sign`` op-cert that carries no kind-prefixed
# manifest[0]. Such a cert is UNGATED (the merge-gate only verifies plan-review / completion), so
# the label is never checked on the read path — the shape-aware wrapper selects the scheme by the
# fixed ``OPCERT_KIND`` policy, not the subject kind.
OPCERT_GENERIC_KIND = "attestation"


class OpcertKeyUnavailable(Exception):
    """The op-cert signature cannot be produced AND cannot be recovered — a missing/too-old
    ``ssh-keygen`` (OpenSSH < 8.9) or an unwritable tracker dir. This is the DEGRADE signal: the
    seam converts it to a :class:`rebar.signing.SigningError` so a signing call site records the
    in-band ``{signed: false}`` outcome (no local op is wedged) and a gate that REQUIRES the
    signature blocks with an OpenSSH ≥ 8.9 remediation."""

    def __init__(self, message: str, returncode: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.returncode = returncode


# ── environment key custody (asymmetric Ed25519) ──────────────────────────────
def opcert_key_path(tracker: str | os.PathLike[str]) -> str:
    """The environment's op-cert PRIVATE key path (``<tracker>/.opcert-key``)."""
    return str(Path(tracker) / OPCERT_KEY_FILE)


def _derive_opcert_pub(key_path: str) -> None:
    """(Re)derive ``<key_path>.pub`` from the committed private key via ``ssh-keygen -y``.

    The public key is DERIVATIVE — never a commit point — so it is safe to (re)write any time it
    is missing. Best-effort: a failure to derive is logged and swallowed (the private key is the
    authority; verification re-derives on demand)."""
    pub_path = key_path + ".pub"
    try:
        proc = subprocess.run(["ssh-keygen", "-y", "-f", key_path], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        logger.debug(
            "could not derive op-cert public key %s (best-effort)", pub_path, exc_info=True
        )
        return
    tmp = f"{pub_path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(proc.stdout)
        os.replace(tmp, pub_path)  # derivative artifact — os.replace is fine (not a commit point)
    except OSError:
        logger.debug("could not write op-cert public key %s (best-effort)", pub_path, exc_info=True)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _ensure_opcert_pub(key_path: str) -> str:
    """Ensure ``<key_path>.pub`` exists (re-derive it from the private key if absent); return it."""
    pub_path = key_path + ".pub"
    if not os.path.exists(pub_path):
        _derive_opcert_pub(key_path)
    return pub_path


def _generate_opcert_key(key_path: str) -> None:
    """Race-safe genesis of the environment's Ed25519 op-cert key at ``key_path``.

    ``ssh-keygen -f <path>`` writes BOTH ``<path>`` and ``<path>.pub`` (it cannot write to an fd),
    so we generate into a private 0700 ``mkdtemp`` staging dir and then ``os.link`` the PRIVATE key
    into place as the SINGLE exclusive-create commit point: ``os.link`` fails with ``EEXIST`` if a
    concurrent first-signer already committed a key, and the loser ADOPTS the winner's key by
    re-reading the existing file (never ``os.replace``, which is not exclusive and would clobber a
    concurrently-committed key). The ``.pub`` is DERIVATIVE — written from the committed private key
    via ``ssh-keygen -y`` — never a commit point. The staging dir is removed in ``finally``."""
    import shutil
    import tempfile

    from rebar.attest import sshsig

    # SshKeygenUnavailable → degrade path (mint converts it to OpcertKeyUnavailable).
    sshsig.ensure_available()
    try:
        staging = tempfile.mkdtemp(prefix=".opcert-key.", dir=str(Path(key_path).parent))
    except OSError as exc:
        raise OpcertKeyUnavailable(
            f"Error: could not create op-cert key staging dir (tracker unwritable?): {exc}"
        ) from None
    try:
        staging_priv = os.path.join(staging, "key")
        proc = subprocess.run(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                staging_priv,
                "-q",
                "-C",
                "rebar-opcert",
            ],
            capture_output=True,
        )
        if proc.returncode != 0 or not os.path.exists(staging_priv):
            raise OpcertKeyUnavailable(
                "Error: ssh-keygen failed to generate the op-cert key: "
                f"{proc.stderr.decode('utf-8', 'replace').strip()}"
            )
        os.chmod(staging_priv, 0o600)
        try:
            os.link(staging_priv, key_path)  # SINGLE exclusive-create commit point
        except FileExistsError:
            pass  # a concurrent first-signer won; adopt its key (re-read on return — no overwrite)
        except OSError as exc:
            raise OpcertKeyUnavailable(
                f"Error: could not commit op-cert key at {key_path}: {exc}"
            ) from None
        _ensure_opcert_pub(key_path)  # derivative — re-derivable from the committed private key
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def ensure_opcert_key(tracker: str | os.PathLike[str], *, create_if_missing: bool = True) -> str:
    """Resolve the environment's op-cert PRIVATE key path, generating it race-safely on first use.

    With ``create_if_missing=True`` (signing) a missing key is generated via
    :func:`_generate_opcert_key`; a deleted/unreadable key is thereby regenerated on the next sign
    (a fresh keypair under the same principal — the same stale-attestation lifecycle as any other).
    With ``create_if_missing=False`` (VERIFY side) a missing key is NEVER created — verification is
    read-only and must not write a secret to disk. Raises :class:`OpcertKeyUnavailable` when the key
    is absent and cannot be created (verify side, or an unwritable tracker)."""
    key_path = opcert_key_path(tracker)
    if os.path.exists(key_path):
        _ensure_opcert_pub(key_path)
        return key_path
    if not create_if_missing:
        raise OpcertKeyUnavailable(
            f"Error: op-cert key {key_path} is absent (the verify side never creates a key)"
        )
    _generate_opcert_key(key_path)
    return key_path


def opcert_principal(tracker: str | os.PathLike[str]) -> str:
    """The DSSE principal (SSHSIG keyid) op-certs are signed under: ``REBAR_OPCERT_ENV_ID`` when set
    (deployment override), else the store's ``.env-id`` (via ``_seam.env_id``)."""
    override = os.environ.get("REBAR_OPCERT_ENV_ID")
    if override and override.strip():
        return override.strip()
    from rebar._commands._seam import env_id as _env_id

    return _env_id(Path(tracker))


def _opcert_own_public_key(tracker: str | os.PathLike[str]) -> str | None:
    """The environment's own op-cert public-key line (``ssh-ed25519 AAAA… rebar-opcert``), or None.

    Read-only: never CREATES the private key. If the ``.pub`` is missing but the private key is
    present, re-derive the public key (derivative artifact); if neither is present, return None."""
    key_path = opcert_key_path(tracker)
    pub_path = key_path + ".pub"
    try:
        text = Path(pub_path).read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    if os.path.exists(key_path):  # re-derive the derivative .pub from the committed private key
        _derive_opcert_pub(key_path)
        try:
            text = Path(pub_path).read_text(encoding="utf-8").strip()
            return text or None
        except OSError:
            return None
    return None


def _manifest_material_fingerprint(manifest) -> str | None:
    """Extract the bound ``material: <fingerprint>`` value from a manifest (the material both the
    plan-review and completion manifests carry), or None when absent."""
    for step in manifest or []:
        if isinstance(step, str) and step.startswith("material:"):
            return step.split(":", 1)[1].strip()
    return None


# ── mint (write-new) ──────────────────────────────────────────────────────────
def mint_opcert_record(resolved: str, steps: list[str], *, kind: str | None, repo_root) -> dict:
    """Build the envelope-bearing SIGNATURE record for ``resolved`` (NOT persisted here).

    Mints a ``rebar.opcert.v1`` DSSE op-cert with the environment's Ed25519 key, deriving the values
    the caller does not supply: the DSSE ``principal`` (``REBAR_OPCERT_ENV_ID`` else ``.env-id``);
    the material fingerprint from the manifest's ``material:`` line; the bound commit from the
    manifest's ``verified-at-sha`` line, else current ``HEAD``. Raises :class:`OpcertKeyUnavailable`
    on the degrade path (missing/too-old ssh-keygen, or an unwritable tracker)."""
    from rebar.attest import dsse, opcert, sshsig
    from rebar.reducer._processors import attestation_kind
    from rebar.signing import head_sha, verified_at_sha_from_manifest

    tracker = config.tracker_dir(repo_root)
    try:
        sshsig.ensure_available()
    except sshsig.SshKeygenUnavailable as exc:
        raise OpcertKeyUnavailable(f"Error: cannot mint op-cert signature: {exc}") from None
    key_path = ensure_opcert_key(str(tracker), create_if_missing=True)

    principal = opcert_principal(str(tracker))
    material_fingerprint = _manifest_material_fingerprint(steps) or ""
    # Bound commit: the manifest's signed `verified-at-sha:` when present (an attested review or
    # close), else current HEAD.
    merged_log_commit = verified_at_sha_from_manifest(steps) or head_sha(
        config.repo_root(repo_root)
    )
    # Subject kind: manifest[0] is authoritative for gated kinds (plan-review / completion); fall
    # back to the caller hint, then a generic label for a raw `rebar sign` (ungated).
    subject_kind = (
        attestation_kind(steps, {"kind": kind} if kind else {}) or kind or OPCERT_GENERIC_KIND
    )

    env = opcert.sign_opcert(
        resolved,
        material_fingerprint,
        merged_log_commit,
        kind=subject_kind,
        key_path=key_path,
        principal=principal,
        # Bind the full manifest into the SIGNED payload so the plan-review freshness checks
        # (stale-code via manifest_deps, stale-regver via manifest_regver) read authenticated
        # inputs, not the attacker-writable plaintext record manifest.
        manifest=steps,
    )
    envelope = dsse.encode(
        env.payload_type,
        env.payload,
        [{"keyid": s.keyid, "sig": s.sig} for s in env.signatures],
    )
    record = {
        "manifest": steps,
        "algorithm": "sshsig",
        "envelope": envelope,
        "material_fingerprint": material_fingerprint,
        "merged_log_commit": merged_log_commit,
        "principal": principal,
        # Retained so the plan-review claim gate's UNSCOPED whole-HEAD freshness check
        # (compute_validity) works identically to the legacy HMAC record.
        "head_sha": head_sha(config.repo_root(repo_root)),
    }
    if kind is not None:
        record["kind"] = kind
    return record


# ── verify (same-environment certification) ───────────────────────────────────
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


def verify_opcert_record(
    record: dict, ticket_id: str, *, kind: str | None = None, repo_root=None
) -> dict:
    """Verify an ``envelope``-bearing op-cert record for SAME-ENVIRONMENT certification.

    Translation table (the wrapper's contract; downstream readers see the uniform verify shape):
      * ``record.principal != own env_id`` → verdict ``foreign_key`` / ``certified: false`` WITHOUT
        invoking the scheme (there is no key to verify with — exactly today's HMAC ``foreign_key``
        semantics; a cert for another environment is the merge-gate's job, not the local wrapper's);
      * ``record.principal == own env_id`` → ``registry.verify(OPCERT_KIND, envelope, trust_root)``
        against the environment's OWN public key, THEN the SIGNED subject-binding check
        (:func:`_opcert_subject_binding_error`), mapping ``verified → certified`` and passing the
        scheme ``verdict``/``reason`` through (``certified`` / ``mismatch`` / ``invalid`` /
        ``unavailable`` / ``unknown_kind`` / ``unknown_scheme``).

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
    }

    if envelope is None:
        # A malformed / undecodable envelope fails CLOSED (never certified).
        return {
            **base,
            "verified": False,
            "verdict": "invalid",
            "reason": "op-cert envelope could not be decoded",
        }

    # Foreign principal: signed by (or claiming) a DIFFERENT environment — cannot certify here.
    if not principal or principal != own_principal:
        return {
            **base,
            "verified": False,
            "verdict": "foreign_key",
            "reason": (
                f"op-cert signed by a different environment "
                f"(principal {principal!r}; this environment is {own_principal!r})"
            ),
        }

    own_pub = _opcert_own_public_key(tracker)
    if not own_pub:
        return {
            **base,
            "verified": False,
            "verdict": "foreign_key",
            "reason": "this environment has no op-cert public key; it cannot certify any signature",
        }

    trust_root = authorship.allowed_signers_from_keys([own_pub], principal)
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


def sign_opcert_manifest(
    ticket_id: str,
    manifest,
    *,
    material_fingerprint: str,
    merged_log_commit: str,
    key_path: str,
    principal: str,
    repo_root=None,
) -> dict:
    """Sign a manifest as an ASYMMETRIC op-cert (keystone e4df); append an envelope-bearing
    SIGNATURE event.

    Builds a DSSE envelope via :func:`rebar.attest.opcert.sign_opcert` binding
    ``{ticket_id, material_fingerprint, merged_log_commit}``, then appends a SIGNATURE event whose
    record carries the encoded ``envelope`` + those bound fields + ``algorithm="sshsig"`` and the
    signed ``manifest`` (first line ``"<kind>: …"`` so the reducer derives the attestation kind) —
    but NO HMAC ``signature``. The kind-keyed ``attestations`` map then holds an op-cert record the
    merge-gate (4214) verifies. Re-exported as ``rebar.signing.sign_opcert_manifest``."""
    import time

    from rebar._commands._seam import (
        CommandError,
        append_event,
        require_id,
        require_not_ghost,
    )
    from rebar.attest import opcert
    from rebar.attest.dsse import encode
    from rebar.reducer._processors import attestation_kind
    from rebar.signing import SigningError, parse_manifest

    if not ticket_id:
        raise SigningError("Error: ticket_id must be non-empty")
    steps = parse_manifest(manifest)

    tracker = config.tracker_dir(repo_root)
    try:
        resolved = require_id(ticket_id, tracker)
        require_not_ghost(resolved, tracker)
    except CommandError as exc:
        raise SigningError(exc.message, exc.returncode) from None

    # The attestation kind (from the manifest) is bound INTO the signed op-cert subject, so a cert
    # cannot be filed under / accepted for a different kind than it was signed for (kind-confusion).
    kind = attestation_kind(steps, {})
    if kind is None:
        raise SigningError("Error: op-cert manifest[0] must encode a kind (e.g. 'plan-review: …')")
    env = opcert.sign_opcert(
        resolved,
        material_fingerprint,
        merged_log_commit,
        kind=kind,
        key_path=key_path,
        principal=principal,
        # Bind the full manifest into the SIGNED payload (see mint_opcert_record) so downstream
        # freshness checks read authenticated dep-hashes / regver, not the plaintext mirror.
        manifest=steps,
    )
    envelope = encode(
        env.payload_type,
        env.payload,
        [{"keyid": s.keyid, "sig": s.sig} for s in env.signatures],
    )
    record = {
        "manifest": steps,
        "algorithm": "sshsig",
        "envelope": envelope,
        "material_fingerprint": material_fingerprint,
        "merged_log_commit": merged_log_commit,
        "signed_at": time.time_ns(),
        # Unsigned routing hint mirroring the manifest-authoritative kind the reducer derives
        # (the kind is ALSO bound into the signed envelope subject above).
        "kind": kind,
    }
    try:
        append_event(resolved, "SIGNATURE", record, tracker, repo_root=repo_root)
    except CommandError as exc:
        raise SigningError(exc.message, exc.returncode) from None
    return {**record, "ticket_id": resolved}
