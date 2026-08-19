"""Environment-bound manifest signing for tickets — the entry module of the signing family.

New writes mint ``rebar.opcert.v1`` DSSE op-certs with an environment Ed25519 key; reads dispatch
by record shape so legacy HMAC attestations remain verifiable. Signatures persist as append-only
``SIGNATURE`` events through the shared locked write path. Legacy HMAC keys come from
``REBAR_SIGNING_KEY`` or ``<tracker>/.signing-key`` and bind the ticket id and whole manifest.

This module owns the concerns that need the whole picture — scheme DISPATCH
(:func:`verify_attestation_record`), the op-cert sign path, the resolve/reduce read path, and the
two CLI arms — and re-exports the layers beneath it so ``rebar.signing`` stays the single import
point for the entire surface (story f5c1-e41d split it along the seams that already existed):

* :mod:`rebar._signing_manifest` — the signed-manifest vocabulary + gate-code provenance (a
  stdlib-only leaf).
* :mod:`rebar._signing_hmac` — the retired symmetric HMAC scheme, kept whole.
* :mod:`rebar._opcert_signing` — the op-cert env-key custody + mint/verify machinery (story 8d8e).

Direction is ``signing -> _signing_hmac -> _signing_manifest``, acyclic; there are no back-edges.
A re-exported symbol is an ALIAS: its in-family consumers resolve it in its DEFINING module, so
tests must monkeypatch it there, not here — see those modules' docstrings and
``tests/unit/test_signing_module_split.py``, which guards both seams positively.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

from rebar import config
from rebar._opcert_signing import (
    OpcertKeyUnavailable,
    ensure_opcert_key,
    mint_opcert_record,
    opcert_principal,
    sign_opcert_manifest,
    verify_opcert_record,
)
from rebar._signing_hmac import (
    _NO_KEY,
    ALGORITHM,
    PAYLOAD_VERSION,
    _canonical_payload,
    _generate_key_file,
    _hmac_opcert_not_certified,
    compute_signature,
    key_fingerprint,
    signing_key,
    verify_record,
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

# The re-export contract: ``rebar.signing`` is the single import point for the whole family, so
# every name the sibling modules define is listed here — the PRIVATE ones deliberately, because
# they are reached by `rebar/llm/build_drift.py` (`_gate_commit_sha`) and by tests, and without
# the declaration a linter reads each re-export as an unused import and an auto-fix deletes it.
# Sorted (RUF022), not grouped by origin — the import block above shows which module owns what.
__all__ = [
    "ALGORITHM",
    "OPCERT_KINDS",
    "PAYLOAD_VERSION",
    "REBAR_VERSION_PREFIX",
    "VERIFIED_AT_SHA_PREFIX",
    "_NO_KEY",
    "OpcertKeyUnavailable",
    "SigningError",
    "_baked_commit_sha",
    "_canonical_payload",
    "_gate_commit_sha",
    "_gate_source_dir",
    "_generate_key_file",
    "_hmac_opcert_not_certified",
    "compute_signature",
    "ensure_opcert_key",
    "gate_code_version",
    "head_sha",
    "key_fingerprint",
    "most_recent_attestation",
    "opcert_principal",
    "parse_manifest",
    "rebar_version_from_manifest",
    "rebar_version_step",
    "sign_manifest",
    "sign_opcert_manifest",
    "signing_key",
    "verified_at_sha_from_manifest",
    "verified_at_sha_step",
    "verified_at_sha_subject",
    "verify_attestation_record",
    "verify_attestations",
    "verify_record",
    "verify_signature",
]

# The gated OP-CERT kinds (story 8f1d, contract phase): signed + accepted EXCLUSIVELY as
# asymmetric op-certs (rebar.opcert.v1 DSSE/SSHSIG). The legacy symmetric HMAC scheme is retired
# for them (a shared HMAC secret would let any verifier forge a verdict). The generic HMAC utility
# (compute_signature / verify_record / .signing-key) is UNCHANGED for non-op-cert consumers.
OPCERT_KINDS = frozenset({"plan-review", "completion-verifier"})


# ── Verify dispatch (which scheme certified this record) ──────────────────────
def verify_attestation_record(
    record: dict | None,
    ticket_id: str,
    *,
    kind: str | None = None,
    key: bytes | None = None,
    repo_root=None,
) -> dict:
    """Shape-aware verify dispatch (story 8d8e, expand phase = write-new / read-both).

    An ``envelope``-bearing record (a ``rebar.opcert.v1`` DSSE op-cert) routes to the op-cert
    verifier; a legacy ``signature``-bearing (HMAC) record routes to the UNCHANGED
    :func:`rebar._signing_hmac.verify_record`. This lets old HMAC attestations verify alongside
    new envelope ones on the same store (kind-keyed coexistence). The result shape is the uniform
    :func:`verify_record` contract either way, so downstream readers (``verify_signature``,
    ``compute_validity``, ``signature_findings``) are unchanged.

    ``kind`` is the attestation-kind SLOT this record was fetched from; it is threaded to the
    op-cert verifier so it can enforce the SIGNED subject binding (cross-ticket / cross-kind replay
    defense, finding A). It is irrelevant to the legacy HMAC path. ``key`` is the HMAC secret for
    the legacy path; when omitted it is resolved read-only (a missing key never mints one).
    ``repo_root`` locates the tracker for the op-cert principal + pubkey.

    Contract phase (story 8f1d): the ``OPCERT_KINDS`` no longer accept the legacy HMAC scheme.
    A non-envelope (HMAC) record whose effective kind is an op-cert kind reads NOT-certified
    (``unknown_scheme``, validity-on-read, record never mutated); re-running the gate re-issues an
    asymmetric op-cert. Non-op-cert HMAC records still verify through :func:`verify_record`."""
    record = record if isinstance(record, dict) else {}
    if record.get("envelope"):
        return verify_opcert_record(record, ticket_id, kind=kind, repo_root=repo_root)
    # A legacy HMAC record: resolve its effective kind (explicit slot, else the SIGNED manifest[0]
    # the reducer keys on) and refuse a genuine HMAC signature for the op-cert kinds.
    from rebar.reducer._processors import attestation_kind

    effective_kind = kind if kind is not None else attestation_kind(record.get("manifest"), record)
    if effective_kind in OPCERT_KINDS and record.get("signature"):
        return _hmac_opcert_not_certified(record, effective_kind)
    if key is None:
        key = signing_key(str(config.tracker_dir(repo_root)), create_if_missing=False)
    return verify_record(record, ticket_id, key)


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
    """Sign a manifest of verified steps for a ticket; append a SIGNATURE event.
    Delegates to :func:`rebar._opcert_signing.mint_opcert_record` (one Ed25519 DSSE op-cert per
    verdict, persisted through the locked write path); a degraded/invalid sign raises
    :class:`SigningError`, recorded by callers as an in-band ``{signed: false}`` outcome. ``kind``
    is an UNSIGNED routing hint (authoritative kind is signed ``manifest[0]``); ``signer`` (story
    6f14) is an OPTIONAL startup op-cert binding forwarded to the mint (omit for env/genesis)."""
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

    # DEGRADE path: a missing/too-old ssh-keygen, or a key that cannot be (re)generated, raises a
    # SigningError naming OpenSSH >= 8.9. Callers record it as an in-band {signed: false} outcome —
    # no local op is wedged by signing itself (the gate that needs it blocks with the remediation).
    try:
        record = mint_opcert_record(resolved, steps, kind=kind, repo_root=repo_root, binding=signer)
    except OpcertKeyUnavailable as exc:
        raise SigningError(
            f"{exc.message}. Install OpenSSH >= 8.9 and ensure the tracker directory is writable."
        ) from None
    record["signed_at"] = time.time_ns()
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
    """Shared verify boilerplate: resolve the id, reduce the ticket, load the key.

    Verify is a READ: never mint a key on disk (a read-only deployment must not write a
    secret). A key-less environment can only ever report unsigned/foreign_key — the honest
    answer. Returns ``(resolved_id, state, key)``; raises :class:`SigningError` (exit 1) when
    the ticket cannot be resolved."""
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
    return resolved, state, signing_key(tracker, create_if_missing=False)


def most_recent_attestation(state: dict):
    """The most-recent signed attestation of ANY kind — the semantics the legacy
    single-slot ``state['signature']`` mirror provided, now sourced from the kind-keyed
    ``state['attestations']`` map (352b contract phase). "Most recent" = the record with
    the greatest ``signed_at`` (ties broken by iteration/replay order, so the last-processed
    wins — matching the mirror's last-writer-wins).

    Falls back to the legacy ``state['signature']`` mirror only when the map is absent/empty
    — e.g. a pre-attestations snapshot the read-side fold-in did not populate. Post-feature
    snapshots always carry ``attestations`` (and old snapshots are folded into it on read),
    so the fallback is a defensive belt-and-suspenders, not the common path."""
    att = state.get("attestations")
    if isinstance(att, dict) and att:
        # max() keeps the LAST max on ties (stable), i.e. the last-processed of equal
        # signed_at — preserving the mirror's replay-order last-writer-wins.
        return max(att.values(), key=lambda r: (r or {}).get("signed_at") or "")
    return state.get("signature")


def _record_for_kind(state: dict, kind: str | None):
    """The signature record to verify for ``kind``. ``kind=None`` returns the most-recent
    attestation of any kind (via :func:`most_recent_attestation` — the pre-attestations
    "verify the latest signature" behavior, now map-sourced). An explicit kind returns
    ``state['attestations'][kind]`` STRICTLY (None when that kind is absent → an honest
    ``unsigned``); a different-kind record is never substituted for a requested kind."""
    if kind is None:
        return most_recent_attestation(state)
    att = state.get("attestations")
    return att.get(kind) if isinstance(att, dict) else None


def verify_signature(ticket_id: str, *, kind: str | None = None, repo_root=None) -> dict:
    """Certify a ticket's recorded verified-steps against its signature.

    Resolves the id, reduces the ticket, and verifies one signature record with the
    environment key. Returns the verdict dict (see :func:`verify_record`) with the resolved
    ``ticket_id`` attached. Raises :class:`SigningError` (exit 1) only when the ticket itself
    cannot be resolved.

    ``kind`` selects WHICH attestation to verify (epic dark-acme-lumen): ``None`` (default)
    verifies the legacy most-recent ``signature`` mirror — exact pre-attestations behavior, so
    every existing no-kind caller is unchanged — while an explicit kind (e.g. ``"plan-review"``
    / ``"completion-verifier"``) verifies THAT kind strictly from the kind-keyed map. Use
    :func:`verify_attestations` for all kinds at once."""
    resolved, state, key = _resolve_and_reduce(ticket_id, repo_root)
    result = verify_attestation_record(
        _record_for_kind(state, kind), resolved, kind=kind, key=key, repo_root=repo_root
    )
    result["ticket_id"] = resolved
    if kind is not None:
        result["kind"] = kind
    return result


def verify_attestations(ticket_id: str, *, repo_root=None) -> dict:
    """Verify EVERY attestation kind on a ticket: returns ``{kind: verdict_dict}`` (each a
    :func:`verify_record` result with ``ticket_id`` + ``kind`` attached), kinds sorted. ``{}``
    when the ticket carries no attestations. No LLM, no network — a pure local HMAC verify per
    kind. Raises :class:`SigningError` only when the ticket cannot be resolved."""
    resolved, state, key = _resolve_and_reduce(ticket_id, repo_root)
    att = state.get("attestations")
    out: dict = {}
    if isinstance(att, dict):
        for k in sorted(att):
            r = verify_attestation_record(att[k], resolved, kind=k, key=key, repo_root=repo_root)
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
        sys.stdout.write(json.dumps(record, ensure_ascii=False) + "\n")
    elif record.get("envelope"):
        # Op-cert record (story 8d8e): render the DSSE envelope digest + principal (there is no
        # HMAC ``signature`` field to slice — the legacy render would KeyError).
        digest = hashlib.sha256(record["envelope"].encode("utf-8")).hexdigest()[:16]
        sys.stdout.write(
            f"SIGNED {record['ticket_id']} "
            f"steps={len(record['manifest'])} "
            f"principal={record.get('principal')} "
            f"envelope={digest}…\n"
        )
    else:
        sys.stdout.write(
            f"SIGNED {record['ticket_id']} "
            f"steps={len(record['manifest'])} "
            f"key={record['key_id']} "
            f"sig={record['signature'][:16]}…\n"
        )
    return 0


def verify_signature_cli(argv: list[str]) -> int:
    """``rebar verify-signature <ticket_id> [--kind <kind>] [--output json]``.

    Verifies a SINGLE attestation and returns its verdict (json = the verdict dict, report =
    one ``SIGNATURE: …`` line) — exit 0 iff ``certified``; exit 1 for
    mismatch/foreign_key/unsigned; the SigningError exit code on an unresolved ticket.
    Without ``--kind`` this verifies the most-recent signature (exact pre-attestations
    behavior). With ``--kind K`` (``--kind K`` or ``--kind=K``) it verifies that kind strictly
    from the kind-keyed map — the per-ticket CI-gate form (epic dark-acme-lumen). The full
    per-kind set is available via the library ``verify_attestations`` and the ``attestations``
    field of ``rebar show``.
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
                json.dumps(
                    error_envelope("ticket_not_found", pos[0], exc.message, exc.returncode),
                    ensure_ascii=False,
                )
                + "\n"
            )
        sys.stderr.write(exc.message + "\n")
        return exc.returncode
    if fmt == "json":
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    else:
        label = f"SIGNATURE[{kind}]" if kind else "SIGNATURE"
        sys.stdout.write(f"{label}: {result['verdict']} — {result['reason']}\n")
    return 0 if result["verified"] else 1
