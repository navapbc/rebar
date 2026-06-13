"""Environment-bound manifest signing for tickets.

This is the cryptographic-attestation surface: a ticket can carry a **manifest of
verified steps** plus an **HMAC-SHA256 signature** computed with a key that is
**specific to the environment** rebar runs in (e.g. an MCP server deployed in a
shared environment). The ``verify-signature`` command then *certifies* that the
recorded steps still match the signature — and, because the key never leaves the
environment, that the signature was produced *here* and not transplanted from
another clone.

Design (mirrors the existing ``.closure-key`` verdict-hash gate in
``compute-verdict-hash.sh``):

* **Key resolution** — ``REBAR_SIGNING_KEY`` (injected out-of-band into a shared
  deployment) wins; otherwise the per-environment ``<tracker>/.signing-key`` file
  (a UUID4 generated on first use, gitignored, never committed, never shared).
  The key is the environment's secret: only a process holding it can produce — or
  certify — a signature.
* **Signed payload** — a canonical JSON serialisation of
  ``{v, algorithm, ticket_id, manifest}``. Binding the ``ticket_id`` stops a
  signature being replayed onto another ticket; binding the whole manifest means
  any edit to the verified-step list invalidates the signature. The signed payload
  deliberately does NOT include volatile git state, so a signature stays
  certifiable as the repo evolves (``head_sha`` is recorded as audit metadata
  only).
* **Key fingerprint** (``key_id``) — a domain-separated SHA-256 prefix of the key,
  stored on the record so verification can distinguish "manifest tampered" from
  "signed by a *different* environment's key" without ever exposing the key.

The signature is persisted as a ``SIGNATURE`` event (append-only, replayed into
``state['signature']`` as last-writer-wins), so it flows through the same locked
write path, auto-push, and compaction as every other event.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import time
import uuid as _uuid
from pathlib import Path

from rebar import config

# HMAC over SHA-256. Recorded on every signature so a future algorithm migration
# is detectable on old records rather than silently mis-verified.
ALGORITHM = "HMAC-SHA256"

# Payload schema version (independent of the event SCHEMA_VERSION): bump only if
# the canonical signed-payload shape changes, since that would invalidate every
# prior signature.
PAYLOAD_VERSION = 1


class SigningError(Exception):
    """A signing/verification failure carrying a stderr message + exit code.

    Mirrors ``rebar._commands._seam.CommandError`` so the library facade maps it
    onto ``RebarError`` and the CLI arms reproduce the stderr + exit contract.
    """

    def __init__(self, message: str, returncode: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.returncode = returncode


# ── Key management (environment-specific secret) ──────────────────────────────
# Sentinel returned when a read-only resolution finds no key. It fingerprints
# deterministically and certifies nothing (a real signature can never match it),
# so a verify on a key-less environment yields unsigned/foreign_key — never a
# false certify — without minting a persistent secret as a side effect.
_NO_KEY = b""


def signing_key(
    tracker: str | os.PathLike[str], *, create_if_missing: bool = True
) -> bytes:
    """Resolve the environment's signing key as raw bytes.

    Order: ``REBAR_SIGNING_KEY`` (non-empty after stripping) > the per-environment
    ``<tracker>/.signing-key`` file. With ``create_if_missing=True`` (signing) a
    missing file is generated as a fresh UUID4 (0o600, atomic). With
    ``create_if_missing=False`` (verifying) a missing file is NOT created — the
    function returns the empty ``_NO_KEY`` sentinel so a read-only verify never
    writes a secret to disk. Raises :class:`SigningError` only on a real I/O error.
    """
    # Strip surrounding whitespace so an injected key copied with a trailing
    # newline fingerprints identically to the file form (which also strips).
    env_key = os.environ.get("REBAR_SIGNING_KEY")
    if env_key and env_key.strip():
        return env_key.strip().encode("utf-8")

    key_file = Path(tracker) / ".signing-key"
    if not key_file.exists():
        if not create_if_missing:
            return _NO_KEY
        _generate_key_file(key_file)
    try:
        return key_file.read_text(encoding="utf-8").strip().encode("utf-8")
    except OSError as exc:
        raise SigningError(f"Error: could not read signing key: {exc}") from None


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
        raise SigningError(
            f"Error: could not create signing key at {key_file}: {exc}"
        ) from None
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


# ── Manifest + payload canonicalisation ───────────────────────────────────────
def parse_manifest(payload) -> list[str]:
    """Validate a manifest into a list of non-empty verified-step strings.

    Accepts an already-parsed list or a JSON-array string. Raises
    :class:`SigningError` (exit 1) with a specific message on any shape error,
    mirroring the leaf-write validators' contract.
    """
    if isinstance(payload, list):
        data = payload
    else:
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            raise SigningError("Error: manifest argument is not valid JSON") from None
    if not isinstance(data, list):
        raise SigningError(
            "Error: manifest must be a JSON array of verified-step strings"
        )
    if not data:
        raise SigningError("Error: manifest must contain at least one verified step")
    steps: list[str] = []
    for idx, item in enumerate(data):
        if not isinstance(item, str) or not item.strip():
            raise SigningError(f"Error: manifest[{idx}] must be a non-empty string")
        steps.append(item)
    return steps


def _canonical_payload(ticket_id: str, manifest: list[str]) -> bytes:
    """Deterministic bytes signed/verified: sorted-key compact JSON."""
    return json.dumps(
        {
            "v": PAYLOAD_VERSION,
            "algorithm": ALGORITHM,
            "ticket_id": ticket_id,
            "manifest": manifest,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compute_signature(ticket_id: str, manifest: list[str], key: bytes) -> str:
    """HMAC-SHA256 hex over the canonical ``(ticket_id, manifest)`` payload."""
    return hmac.new(key, _canonical_payload(ticket_id, manifest), hashlib.sha256).hexdigest()


# ── Verification (pure; no I/O) ───────────────────────────────────────────────
def verify_record(record: dict | None, ticket_id: str, key: bytes) -> dict:
    """Certify a stored signature ``record`` against a freshly recomputed HMAC.

    Returns a verdict dict ``{verified, verdict, reason, ...}`` where ``verdict``
    is one of:

    * ``certified``   — the manifest matches the signature under this key.
    * ``mismatch``    — the steps no longer match (manifest altered / bad sig).
    * ``foreign_key`` — signed by a *different* environment's key (cannot certify
      here; the signing environment must verify it).
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
    local_fp = key_fingerprint(key)

    # Every verdict carries the same keys (uniform contract): consumers can read
    # result["manifest"]/["step_count"] regardless of outcome, including unsigned.
    base = {
        "manifest": manifest,
        "step_count": len(manifest),
        "algorithm": record.get("algorithm"),
        "key_id": stored_fp or None,
        "signed_at": record.get("signed_at"),
        "head_sha": record.get("head_sha"),
    }

    if not stored_sig:
        return {**base, "verified": False, "verdict": "unsigned", "reason": "ticket has no signature"}

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
        "reason": "verified steps do NOT match the signature (manifest altered or signature invalid)",
    }


# ── git audit metadata ────────────────────────────────────────────────────────
def head_sha(repo_root) -> str:
    """Current HEAD sha of ``repo_root``, or ``'unknown'`` when unresolvable.

    Recorded on every signature (audit metadata) and recomputed by the close gate
    for its freshness binding — a public helper so that load-bearing integration
    point isn't reaching into a private name. ``'unknown'`` is a sentinel callers
    must treat as "no resolvable HEAD", never as a matchable value."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "unknown"
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and sha else "unknown"


# ── Library-facing operations ─────────────────────────────────────────────────
def sign_manifest(ticket_id: str, manifest, *, repo_root=None) -> dict:
    """Sign a manifest of verified steps for a ticket; append a SIGNATURE event.

    Validates the manifest, resolves the ticket id, computes the HMAC with the
    environment key, and persists the signature record through the single locked
    write path. Returns the record (with the resolved ``ticket_id``). Raises
    :class:`SigningError` on a validation/resolve failure.
    """
    from rebar._commands._seam import (
        CommandError,
        append_event,
        require_id,
        require_not_ghost,
    )

    if not ticket_id:
        raise SigningError("Error: ticket_id must be non-empty")
    steps = parse_manifest(manifest)

    tracker = config.tracker_dir(repo_root)
    try:
        resolved = require_id(ticket_id, tracker)
        require_not_ghost(resolved, tracker)
    except CommandError as exc:
        raise SigningError(exc.message, exc.returncode) from None

    key = signing_key(str(tracker))
    signature = compute_signature(resolved, steps, key)
    record = {
        "manifest": steps,
        "algorithm": ALGORITHM,
        "signature": signature,
        "key_id": key_fingerprint(key),
        "head_sha": head_sha(config.repo_root(repo_root)),
        "signed_at": time.time_ns(),
    }
    try:
        append_event(resolved, "SIGNATURE", record, tracker, repo_root=repo_root)
    except CommandError as exc:
        raise SigningError(exc.message, exc.returncode) from None
    return {**record, "ticket_id": resolved}


def verify_signature(ticket_id: str, *, repo_root=None) -> dict:
    """Certify a ticket's recorded verified-steps against its signature.

    Resolves the id, reduces the ticket, and verifies ``state['signature']`` with
    the environment key. Returns the verdict dict (see :func:`verify_record`) with
    the resolved ``ticket_id`` attached. Raises :class:`SigningError` (exit 1)
    only when the ticket itself cannot be resolved.
    """
    from rebar._engine_support import reads
    from rebar._engine_support.resolver import resolve_ticket_id
    from rebar.reducer import reduce_ticket

    if not ticket_id:
        raise SigningError("Error: ticket_id must be non-empty")
    tracker = str(config.tracker_dir(repo_root))
    reads.ensure_fresh(tracker)
    resolved = resolve_ticket_id(ticket_id, tracker)
    if resolved is None:
        raise SigningError(f"Error: ticket '{ticket_id}' not found")
    state = reduce_ticket(os.path.join(tracker, resolved)) or {}
    # Verify is a READ: never mint a key on disk (a read-only deployment must not
    # write a secret). A key-less environment can only ever report unsigned/
    # foreign_key, which is the honest answer.
    key = signing_key(tracker, create_if_missing=False)
    result = verify_record(state.get("signature"), resolved, key)
    result["ticket_id"] = resolved
    return result


# ── CLI arms (in-process dispatch from rebar._cli) ────────────────────────────
def sign_cli(argv: list[str]) -> int:
    """``rebar sign <ticket_id> <manifest_json> [--output json]``."""
    import sys

    from rebar._engine_support.output import OutputFormatError, parse_output

    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    if len(rest) < 2:
        sys.stderr.write("Usage: rebar sign <ticket_id> <manifest_json>\n")
        return 1
    try:
        record = sign_manifest(rest[0], rest[1])
    except SigningError as exc:
        sys.stderr.write(exc.message + "\n")
        return exc.returncode
    if fmt == "json":
        sys.stdout.write(json.dumps(record, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(
            f"SIGNED {record['ticket_id']} "
            f"steps={len(record['manifest'])} "
            f"key={record['key_id']} "
            f"sig={record['signature'][:16]}…\n"
        )
    return 0


def verify_signature_cli(argv: list[str]) -> int:
    """``rebar verify-signature <ticket_id> [--output json]``.

    Exit 0 iff the verdict is ``certified``; exit 1 for mismatch / foreign_key /
    unsigned / unresolved ticket.
    """
    import sys

    from rebar._engine_support.output import (
        OutputFormatError,
        error_envelope,
        parse_output,
    )

    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    if len(rest) < 1:
        sys.stderr.write("Usage: rebar verify-signature <ticket_id>\n")
        return 1
    try:
        result = verify_signature(rest[0])
    except SigningError as exc:
        if fmt == "json":
            sys.stdout.write(
                json.dumps(
                    error_envelope("ticket_not_found", rest[0], exc.message, exc.returncode),
                    ensure_ascii=False,
                )
                + "\n"
            )
        sys.stderr.write(exc.message + "\n")
        return exc.returncode
    if fmt == "json":
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(f"SIGNATURE: {result['verdict']} — {result['reason']}\n")
    return 0 if result["verified"] else 1
