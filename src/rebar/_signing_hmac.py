"""The legacy symmetric HMAC signing scheme, kept whole.

New writes mint asymmetric ``rebar.opcert.v1`` DSSE op-certs (``rebar._opcert_signing``);
this module is the RETIRED symmetric scheme that still has to verify every attestation
written before that switch. It owns the whole HMAC concern end to end — per-environment key
custody, the canonical signed payload, ``compute_signature``, and the ``verify_record``
verdict — so nothing about the old scheme is spread across modules. Split out of
``signing.py`` (story f5c1-e41d); ``rebar.signing`` re-exports everything below, so importers
are unchanged.

Sits between :mod:`rebar._signing_manifest` (the vocabulary it reads) and :mod:`rebar.signing`
(the dispatcher that routes to it). It never imports ``rebar.signing`` — the direction
``signing -> _signing_hmac -> _signing_manifest`` is acyclic by construction.

**Patch these symbols HERE, not on ``rebar.signing``.** ``verify_record`` and
``_hmac_opcert_not_certified`` read ``verified_at_sha_from_manifest`` /
``rebar_version_from_manifest`` as bare globals, i.e. out of THIS module's namespace, so a
patch on the ``rebar.signing`` alias would silently not reach them.
``tests/unit/test_signing_module_split.py`` guards that positively.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid as _uuid
from pathlib import Path

from rebar._signing_manifest import (
    SigningError,
    rebar_version_from_manifest,
    verified_at_sha_from_manifest,
)
from rebar._store.canonical import canonical_bytes

# HMAC over SHA-256. Recorded on every signature so a future algorithm migration
# is detectable on old records rather than silently mis-verified.
ALGORITHM = "HMAC-SHA256"

# Payload schema version (independent of the event SCHEMA_VERSION): bump only if
# the canonical signed-payload shape changes, since that would invalidate every
# prior signature.
PAYLOAD_VERSION = 1


# ── Key management (environment-specific secret) ──────────────────────────────
# Sentinel returned when a read-only resolution finds no key. It fingerprints
# deterministically and certifies nothing (a real signature can never match it),
# so a verify on a key-less environment yields unsigned/foreign_key — never a
# false certify — without minting a persistent secret as a side effect.
_NO_KEY = b""


def signing_key(tracker: str | os.PathLike[str], *, create_if_missing: bool = True) -> bytes:
    """Resolve the environment's signing key as raw bytes.

    Order: ``REBAR_SIGNING_KEY`` (non-empty after stripping) > the per-environment
    ``<tracker>/.signing-key`` file. With ``create_if_missing=True`` (signing) a
    missing file is generated as a fresh UUID4 (0o600, atomic). With
    ``create_if_missing=False`` (verifying) a missing file is NOT created — the
    function returns the empty ``_NO_KEY`` sentinel so a read-only verify never
    writes a secret to disk. An empty / whitespace-only key file is treated as
    corruption: a signing caller gets a :class:`SigningError` (an empty key is
    attacker-guessable and must never sign), a verify caller gets ``_NO_KEY``
    (so it certifies nothing). Raises :class:`SigningError` on a real I/O error.
    """
    # Strip surrounding whitespace so an injected key copied with a trailing
    # newline fingerprints identically to the file form (which also strips).
    env_key = os.environ.get("REBAR_SIGNING_KEY")  # read-via: credential-injection
    if env_key and env_key.strip():
        return env_key.strip().encode("utf-8")

    key_file = Path(tracker) / ".signing-key"
    if not key_file.exists():
        if not create_if_missing:
            return _NO_KEY
        _generate_key_file(key_file)
    try:
        raw = key_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SigningError(f"Error: could not read signing key: {exc}") from None
    if not raw:
        # Empty/whitespace-only key file: an empty key is forgeable by anyone, so
        # it must never be used. Read-only verifies degrade to _NO_KEY (certify
        # nothing); a signing caller must fail loudly rather than emit a forgeable
        # signature.
        if not create_if_missing:
            return _NO_KEY
        raise SigningError(
            f"Error: signing key at {key_file} is empty (corrupt). Remove it to "
            "regenerate, or set REBAR_SIGNING_KEY."
        )
    return raw.encode("utf-8")


def _generate_key_file(key_file: Path) -> None:
    """Atomically create ``key_file`` with a fresh UUID4 key (0o600).

    Write the full key to a unique temp (``mkstemp`` → 0o600, O_EXCL, distinct
    per thread AND per process), then ``os.link`` it into place. The link is
    atomic and fails closed if the target already exists, so exactly ONE creator
    ever lands a key and every reader observes the complete file — never the
    empty/torn window an in-place O_EXCL+write would expose, and never two
    divergent keys for one environment (S1). A lost race (target exists) is
    fine: we drop our temp and the caller reads the winner's key.
    """
    import tempfile

    fd, tmp = tempfile.mkstemp(prefix=".signing-key.", dir=str(key_file.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(_uuid.uuid4()) + "\n")
        try:
            os.link(tmp, str(key_file))  # atomic exclusive create
        except FileExistsError:
            pass  # someone else won the race; their key stays
    except OSError as exc:
        raise SigningError(f"Error: could not create signing key at {key_file}: {exc}") from None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def key_fingerprint(key: bytes) -> str:
    """A short, domain-separated SHA-256 fingerprint of the key (never the key).

    Stored on the signature record as ``key_id`` so verification can report
    "signed by a different environment" distinctly from "manifest altered".
    """
    return hashlib.sha256(b"rebar-signing-key-v1\x00" + key).hexdigest()[:16]


# ── Payload canonicalisation ──────────────────────────────────────────────────
def _canonical_payload(ticket_id: str, manifest: list[str]) -> bytes:
    """Deterministic bytes signed/verified: sorted-key compact JSON.

    Routed through the canonical seam (:func:`rebar._store.canonical.canonical_bytes`,
    ``ensure_ascii=False``) — byte-identical to the prior inline ``json.dumps``.
    """
    return canonical_bytes(
        {
            "v": PAYLOAD_VERSION,
            "algorithm": ALGORITHM,
            "ticket_id": ticket_id,
            "manifest": manifest,
        }
    )


def compute_signature(ticket_id: str, manifest: list[str], key: bytes) -> str:
    """HMAC-SHA256 hex over the canonical ``(ticket_id, manifest)`` payload."""
    return hmac.new(key, _canonical_payload(ticket_id, manifest), hashlib.sha256).hexdigest()


# ── Verification (pure; no I/O) ───────────────────────────────────────────────
def _base_verdict(record: dict, manifest: list[str], key_id: str | None) -> dict:
    """The keys EVERY HMAC verdict carries, whatever the outcome (uniform contract):
    consumers read ``result["manifest"]`` / ``["step_count"]`` / the provenance pins
    regardless of whether the record certified, mismatched, or was never signed."""
    return {
        "manifest": manifest,
        "step_count": len(manifest),
        "algorithm": record.get("algorithm"),
        "key_id": key_id,
        "signed_at": record.get("signed_at"),
        "head_sha": record.get("head_sha"),
        # The attested SHA the verdict was computed against (from the signed manifest step;
        # falls back to the record field). None for legacy/non-attested signatures.
        "verified_at_sha": verified_at_sha_from_manifest(manifest) or record.get("verified_at_sha"),
        # The rebar gate code that produced the attestation (audit/provenance, epic
        # jira-reb-596). None for pre-stamp / unsigned records.
        "rebar_version": rebar_version_from_manifest(manifest),
    }


def verify_record(record: dict | None, ticket_id: str, key: bytes) -> dict:
    """Certify a stored signature ``record`` against a freshly recomputed HMAC.

    Returns a verdict dict ``{verified, verdict, reason, ...}`` where ``verdict``
    is one of:

    * ``certified``   — the manifest matches the signature under this key.
    * ``mismatch``    — the steps no longer match (manifest altered / bad sig).
    * ``foreign_key`` — signed by a *different* environment's key, OR this
      environment has no usable key — either way it cannot be certified here.
    * ``unsigned``    — the ticket carries no signature.
    """
    # Fail closed on any malformed record: a non-dict signature value (e.g. a
    # corrupt or forward-compat SNAPSHOT compiled_state) must yield a clean
    # verdict, never an AttributeError that crashes the CLI/MCP caller.
    record = record if isinstance(record, dict) else {}
    raw_manifest = record.get("manifest")
    manifest = raw_manifest if isinstance(raw_manifest, list) else []
    stored_sig = record.get("signature") or ""
    if not isinstance(stored_sig, str):
        stored_sig = ""
    stored_fp = record.get("key_id") or ""
    if not isinstance(stored_fp, str):
        stored_fp = ""

    base = _base_verdict(record, manifest, stored_fp or None)

    if not stored_sig:
        return {
            **base,
            "verified": False,
            "verdict": "unsigned",
            "reason": "ticket has no signature",
        }

    # An empty key (the _NO_KEY sentinel: no .signing-key, no REBAR_SIGNING_KEY, or
    # a corrupt empty key file) can NEVER certify — HMAC under an empty key is
    # forgeable by anyone, so a crafted signature must not be accepted. A key-less
    # environment treats every signature as un-certifiable (foreign).
    if not key:
        return {
            **base,
            "verified": False,
            "verdict": "foreign_key",
            "reason": "this environment has no signing key; it cannot certify any signature",
        }

    local_fp = key_fingerprint(key)
    if stored_fp and stored_fp != local_fp:
        return {
            **base,
            "verified": False,
            "verdict": "foreign_key",
            "reason": (
                f"signature was produced by a different environment key "
                f"(signed with {stored_fp}; this environment is {local_fp})"
            ),
        }

    # No stored fingerprint (a hand-written / forward-compat record) cannot be
    # attributed to an environment — fall through to the HMAC check, which fails
    # CLOSED (mismatch, never certified) when it was actually signed elsewhere.
    expected = compute_signature(ticket_id, manifest, key)
    if hmac.compare_digest(expected, stored_sig):
        return {
            **base,
            "verified": True,
            "verdict": "certified",
            "reason": "verified steps match the signature",
        }
    return {
        **base,
        "verified": False,
        "verdict": "mismatch",
        "reason": (
            "verified steps do NOT match the signature (manifest altered or signature invalid)"
        ),
    }


def _hmac_opcert_not_certified(record: dict, kind: str) -> dict:
    """Uniform not-certified verdict for a legacy HMAC record of an op-cert kind (story 8f1d).

    HMAC is retired for ``OPCERT_KINDS``, so the record can never certify. Same base keys as
    :func:`verify_record` (``compute_validity`` / ``signature_findings`` read it unchanged);
    ``verdict='unknown_scheme'`` — the scheme is no longer accepted for this kind."""
    raw_manifest = record.get("manifest")
    manifest = raw_manifest if isinstance(raw_manifest, list) else []
    key_id = record.get("key_id")
    return {
        **_base_verdict(record, manifest, key_id if isinstance(key_id, str) else None),
        "verified": False,
        "verdict": "unknown_scheme",
        "reason": (
            f"HMAC is a retired scheme for op-cert kind {kind!r}; this legacy HMAC "
            "attestation no longer certifies — re-run the gate to re-issue an asymmetric op-cert"
        ),
    }
