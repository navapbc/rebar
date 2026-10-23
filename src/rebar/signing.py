"""Sign ticket manifests with environment-bound Ed25519 op-certs.

Persist DSSE envelopes as locked ``SIGNATURE`` events. Legacy HMAC records remain
unchanged but read unsigned. This entry module dispatches by record shape and
re-exports manifest and key-custody layers; patch re-exported names at their
defining modules.
"""

from __future__ import annotations

import hashlib
import os
import time

from rebar import config
from rebar._mcp_errors import js_safe_dumps
from rebar._opcert_signing import (
    OpcertKeyUnavailable,
    ensure_opcert_key,
    mint_opcert_record,
    opcert_principal,
    sign_opcert_manifest,
    verify_opcert_record,
)
from rebar._signing_manifest import (
    REBAR_VERSION_PREFIX,
    VERIFIED_AT_SHA_PREFIX,
    SigningError,
    _baked_commit_sha,
    _gate_commit_sha,
    _gate_source_dir,
    gate_code_version,
    head_sha,
    parse_manifest,
    rebar_version_from_manifest,
    rebar_version_step,
    verified_at_sha_from_manifest,
    verified_at_sha_step,
    verified_at_sha_subject,
)

# Keep public and relied-on private aliases here for the single signing import
# point; sorted exports also protect them from unused-import autofixes.
__all__ = [
    "HMAC_RETIRED_ON",
    "LEGACY_HMAC_ALGORITHM",
    "OPCERT_KINDS",
    "REBAR_VERSION_PREFIX",
    "VERIFIED_AT_SHA_PREFIX",
    "OpcertKeyUnavailable",
    "SigningError",
    "_baked_commit_sha",
    "_gate_commit_sha",
    "_gate_source_dir",
    "ensure_opcert_key",
    "gate_code_version",
    "head_sha",
    "most_recent_attestation",
    "opcert_principal",
    "parse_manifest",
    "rebar_version_from_manifest",
    "rebar_version_step",
    "retired_scheme_verdict",
    "sign_manifest",
    "sign_opcert_manifest",
    "verified_at_sha_from_manifest",
    "verified_at_sha_step",
    "verified_at_sha_subject",
    "verify_attestation_record",
    "verify_attestations",
    "verify_signature",
]

# These gated kinds require asymmetric DSSE op-certs; HMAC verification is retired.
OPCERT_KINDS = frozenset({"plan-review", "completion-verifier"})

# The retired symmetric scheme's algorithm tag — recognised on READ only (to word the verdict
# reason for pre-retirement records); never minted.
LEGACY_HMAC_ALGORITHM = "HMAC-SHA256"
HMAC_RETIRED_ON = "2026-09"


def retired_scheme_verdict(record: dict | None) -> dict:
    """Uniform NOT-certified verdict for any non-envelope SIGNATURE record (HMAC retired 2026-09).

    Carries the same base keys as the op-cert verdict (``_opcert_verify``) so ``compute_validity``,
    ``signature_findings`` and ``plan_review_status`` read it unchanged. ``verdict`` is always
    ``unsigned``; the reason names the retired scheme when the record is a legacy HMAC one, else the
    plain no-signature reason. The record is never mutated (validity is computed on read)."""
    record = record if isinstance(record, dict) else {}
    raw = record.get("manifest")
    manifest = raw if isinstance(raw, list) else []
    key_id = record.get("key_id")
    legacy = bool(record.get("signature")) or record.get("algorithm") == LEGACY_HMAC_ALGORITHM
    return {
        "manifest": manifest,
        "step_count": len(manifest),
        "algorithm": record.get("algorithm"),
        "key_id": key_id if isinstance(key_id, str) else None,
        "signed_at": record.get("signed_at"),
        "head_sha": record.get("head_sha"),
        "verified_at_sha": verified_at_sha_from_manifest(manifest) or record.get("verified_at_sha"),
        "rebar_version": rebar_version_from_manifest(manifest),
        "verified": False,
        "verdict": "unsigned",
        "reason": (
            f"legacy HMAC scheme retired ({HMAC_RETIRED_ON}); re-run the gate to mint an op-cert"
            if legacy
            else "ticket has no signature"
        ),
    }


# ── Verify dispatch (which scheme certified this record) ──────────────────────
def verify_attestation_record(
    record: dict | None,
    ticket_id: str,
    *,
    kind: str | None = None,
    repo_root=None,
) -> dict:
    """Verify DSSE envelopes; report other records unsigned in the same shape.

    Pass the selected ``kind`` for signed-subject replay checks and ``repo_root``
    for trusted principal and key lookup.
    """
    if isinstance(record, dict) and record.get("envelope"):
        return verify_opcert_record(record, ticket_id, kind=kind, repo_root=repo_root)
    return retired_scheme_verdict(record)


# ── Sign path (mint an op-cert + append the SIGNATURE event) ──────────────────
def _sign_manifest_under_lock(
    ticket_id: str,
    manifest,
    *,
    kind: str | None = None,
    repo_root=None,
    under_lock_check=None,
    signer=None,
) -> dict:
    """Mint one Ed25519 verdict and append its SIGNATURE under the lock.

    Signing failure raises ``SigningError``. ``kind`` is only a routing hint;
    the manifest signs authority. Forward an optional startup signer binding.
    """
    from rebar._commands._seam import CommandError, append_event

    resolved, record = _prepare_manifest_event(
        ticket_id, manifest, kind=kind, repo_root=repo_root, signer=signer
    )
    tracker = config.tracker_dir(repo_root)
    try:
        append_event(
            resolved,
            "SIGNATURE",
            record,
            tracker,
            repo_root=repo_root,
            under_lock_check=under_lock_check,
        )
    except CommandError as exc:
        raise SigningError(exc.message, exc.returncode) from None
    return {**record, "ticket_id": resolved}


def _prepare_manifest_event(
    ticket_id: str,
    manifest,
    *,
    kind: str | None = None,
    repo_root=None,
    signer=None,
) -> tuple[str, dict]:
    """Mint, but do not append, one SIGNATURE event payload for a caller-owned transaction."""
    from rebar._commands._seam import CommandError, require_id, require_not_ghost

    if not ticket_id:
        raise SigningError("Error: ticket_id must be non-empty")
    steps = parse_manifest(manifest)
    tracker = config.tracker_dir(repo_root)
    try:
        resolved = require_id(ticket_id, tracker)
        require_not_ghost(resolved, tracker)
    except CommandError as exc:
        raise SigningError(exc.message, exc.returncode) from None
    try:
        record = mint_opcert_record(resolved, steps, kind=kind, repo_root=repo_root, binding=signer)
    except OpcertKeyUnavailable as exc:
        raise SigningError(
            f"{exc.message}. Install OpenSSH >= 8.9 and ensure the tracker directory is writable."
        ) from None
    record["signed_at"] = time.time_ns()
    return resolved, record


def sign_manifest(
    ticket_id: str,
    manifest,
    *,
    kind: str | None = None,
    repo_root=None,
    signer=None,
) -> dict:
    """Sign and persist a manifest through the public API; forwards the OPTIONAL ``signer``
    op-cert binding (story 6f14) to the seam (omit to keep the env/genesis behavior)."""
    return _sign_manifest_under_lock(
        ticket_id, manifest, kind=kind, repo_root=repo_root, signer=signer
    )


# ``retire_attested_pin`` was removed; reopen invalidation is computed on read. See ADR 0009.


# ── Read path (resolve the ticket, pick the record, verify it) ────────────────
def _resolve_and_reduce(ticket_id: str, repo_root):
    """Resolve and reduce a ticket, raising ``SigningError`` if absent."""
    if not ticket_id:
        raise SigningError("Error: ticket_id must be non-empty")
    tracker = str(config.tracker_dir(repo_root))
    from rebar._engine_support import reads

    view = reads.current_ticket_view()
    if view is not None:
        try:
            state = view.show_ticket(ticket_id)
        except Exception as exc:  # noqa: BLE001 -- pinned reads fail closed here
            reads.reraise_pinned_read_failure(exc)
            raise SigningError(f"Error: ticket '{ticket_id}' not found ({exc})") from None
        resolved = str(state.get("ticket_id") or "")
        if not resolved:
            raise SigningError(f"Error: ticket '{ticket_id}' not found")
        return resolved, state

    from rebar._engine_support.resolver import resolve_ticket_id
    from rebar.reducer import reduce_ticket

    reads.ensure_fresh(tracker)
    resolved_id = resolve_ticket_id(ticket_id, tracker)
    if resolved_id is None:
        raise SigningError(f"Error: ticket '{ticket_id}' not found")
    state = reduce_ticket(os.path.join(tracker, resolved_id)) or {}
    return resolved_id, state


def most_recent_attestation(state: dict):
    """Select the latest kind-keyed attestation, falling back to the legacy mirror.

    Highest ``signed_at`` wins; replay order breaks ties.
    """
    att = state.get("attestations")
    if isinstance(att, dict) and att:
        # max() keeps the LAST max on ties (stable), i.e. the last-processed of equal
        # signed_at — preserving the mirror's replay-order last-writer-wins.
        return max(att.values(), key=lambda r: (r or {}).get("signed_at") or "")
    return state.get("signature")


def _record_for_kind(state: dict, kind: str | None):
    """Select the latest record or strictly the requested kind, never a substitute."""
    if kind is None:
        return most_recent_attestation(state)
    att = state.get("attestations")
    return att.get(kind) if isinstance(att, dict) else None


def verify_signature(ticket_id: str, *, kind: str | None = None, repo_root=None) -> dict:
    """Verify a ticket's latest or explicitly selected attestation.

    Return the verdict with resolved ticket ID; unresolved tickets raise
    ``SigningError``. Use ``verify_attestations`` to check every kind.
    """
    resolved, state = _resolve_and_reduce(ticket_id, repo_root)
    result = verify_attestation_record(
        _record_for_kind(state, kind), resolved, kind=kind, repo_root=repo_root
    )
    result["ticket_id"] = resolved
    if kind is not None:
        result["kind"] = kind
    return result


def verify_attestations(ticket_id: str, *, repo_root=None) -> dict:
    """Verify each kind locally and return sorted kind-to-verdict entries.

    Return ``{}`` without attestations; unresolved tickets raise ``SigningError``.
    """
    resolved, state = _resolve_and_reduce(ticket_id, repo_root)
    att = state.get("attestations")
    out: dict = {}
    if isinstance(att, dict):
        for k in sorted(att):
            r = verify_attestation_record(att[k], resolved, kind=k, repo_root=repo_root)
            r["ticket_id"] = resolved
            r["kind"] = k
            out[k] = r
    return out


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
        sys.stdout.write(js_safe_dumps(record, ensure_ascii=False) + "\n")
    else:
        # Op-cert record: render the DSSE envelope digest + principal.
        digest = hashlib.sha256(record["envelope"].encode("utf-8")).hexdigest()[:16]
        sys.stdout.write(
            f"SIGNED {record['ticket_id']} "
            f"steps={len(record['manifest'])} "
            f"principal={record.get('principal')} "
            f"envelope={digest}…\n"
        )
    return 0


def verify_signature_cli(argv: list[str]) -> int:
    """CLI check for one latest or ``--kind``-selected signature.

    JSON returns the verdict; report prints one line. Exit 0 only when certified,
    1 for an unsigned or invalid record, or the ticket-resolution error code.
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
    # Parse --kind (accept both the space and equals form, matching the read-CLI convention).
    kind: str | None = None
    pos: list[str] = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--kind" and i + 1 < len(rest):
            kind = rest[i + 1]
            i += 2
            continue
        if a.startswith("--kind="):
            kind = a[len("--kind=") :]
            i += 1
            continue
        pos.append(a)
        i += 1
    if len(pos) < 1:
        sys.stderr.write("Usage: rebar verify-signature <ticket_id> [--kind <kind>]\n")
        return 1
    try:
        result = verify_signature(pos[0], kind=kind)
    except SigningError as exc:
        if fmt == "json":
            sys.stdout.write(
                js_safe_dumps(
                    error_envelope("ticket_not_found", pos[0], exc.message, exc.returncode),
                    ensure_ascii=False,
                )
                + "\n"
            )
        sys.stderr.write(exc.message + "\n")
        return exc.returncode
    if fmt == "json":
        sys.stdout.write(js_safe_dumps(result, ensure_ascii=False) + "\n")
    else:
        label = f"SIGNATURE[{kind}]" if kind else "SIGNATURE"
        sys.stdout.write(f"{label}: {result['verdict']} — {result['reason']}\n")
    return 0 if result["verified"] else 1
