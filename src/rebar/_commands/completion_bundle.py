"""Receipt-aware preparation, publication, and delivery of certified completion closes."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from rebar import config, signing
from rebar._commands import completion_delivery, completion_txn
from rebar._commands._seam import CommandError
from rebar._snapshot.ticket_view import (
    CompletionReadBasis,
    PinnedTicketView,
    ReceiptValidation,
    TicketsOID,
    validate_receipt,
)

_LOCAL_ATTEMPTS = 3
_CODE_OID_PREFIX = "completion-code-oid:"
_TICKETS_OID_PREFIX = "completion-tickets-oid:"
_RECEIPT_PREFIX = "completion-receipt-sha256:"
_RUN_PREFIX = "completion-run:"


@dataclass(frozen=True)
class PreparedCompletionBundle:
    basis: CompletionReadBasis
    verdict_payload: dict[str, Any]
    signature_payload: dict[str, Any]
    verdict_uuid: str
    status_uuid: str
    signature_uuid: str


@dataclass(frozen=True)
class AtomicBundleResult:
    completion_signature: Mapping[str, object]
    atomic_close: Mapping[str, object]


def _elapsed_ms(started_ns: int) -> int:
    return (time.monotonic_ns() - started_ns) // 1_000_000


def _accumulate_metrics(target: dict[str, int], observed: Mapping[str, int]) -> None:
    for key, value in observed.items():
        if key == "atomic_close_events":
            target[key] = value
        else:
            target[key] = target.get(key, 0) + value


def _timed_receipt_validation(
    tracker: str,
    receipt: Mapping[str, Any],
    *,
    current_oid: TicketsOID | None = None,
) -> tuple[ReceiptValidation, int]:
    started = time.monotonic_ns()
    validation = validate_receipt(tracker, receipt, current_oid=current_oid)
    return validation, _elapsed_ms(started)


def has_atomic_basis(result: Mapping[str, Any]) -> bool:
    """Whether a PASS is eligible for the experimental all-or-none close path."""
    return (
        str(result.get("verdict", "")).upper() == "PASS"
        and result.get("source") == "attested"
        and result.get("certifiable") is not False
        and isinstance(result.get("completion_read_basis"), Mapping)
    )


def _publish_close(
    verified_result: dict[str, Any] | None,
    *,
    ticket_id: str,
    tracker: str,
    repo_root,
    ref: str | None,
    env_id: str,
    author: str,
    current_status: str,
    target_status: str,
    close_class: str,
    close_reason: str,
    force_close: str,
    completion_expectation: str,
    pre_status_check: Callable[[Mapping[str, Any]], None] | None,
    legacy_signer: Callable[[dict, str, Any, str | None], dict],
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    """Publish through the atomic bundle when eligible, else retain the legacy close path."""
    from rebar._commands import txn

    completion_signature: dict[str, object] | None = None
    atomic_close: dict[str, object] | None = None
    if verified_result is not None and has_atomic_basis(verified_result):
        bundled = commit_completion_bundle(
            verified_result,
            ticket_id,
            tracker,
            repo_root,
            ref=ref,
            env_id=env_id,
            author=author,
            close_class=close_class,
            close_reason=close_reason,
            completion_expectation=completion_expectation,
            pre_status_check=pre_status_check,
        )
        completion_signature = dict(bundled.completion_signature)
        atomic_close = dict(bundled.atomic_close)

    if atomic_close is None:
        txn.transition_core(
            tracker,
            ticket_id,
            current_status,
            target_status,
            env_id=env_id,
            author=author,
            close_class=close_class,
            close_reason=close_reason,
            force_reason=force_close,
            completion_expectation=completion_expectation,
            repo_root=repo_root,
            pre_status_check=pre_status_check,
        )
        if target_status == "closed" and verified_result is not None:
            completion_signature = legacy_signer(verified_result, ticket_id, repo_root, ref)
    return completion_signature, atomic_close


def _basis_steps(basis: CompletionReadBasis) -> list[str]:
    return [
        f"{_CODE_OID_PREFIX}{basis.code_oid.value}",
        f"{_TICKETS_OID_PREFIX}{basis.tickets_oid.value}",
        f"{_RECEIPT_PREFIX}{basis.receipt_digest}",
        f"{_RUN_PREFIX}{basis.run_id}",
    ]


def verdict_manifest(result: Mapping[str, Any], ticket_id: str, repo_root=None) -> list[str]:
    """Build the deterministic signed manifest for a completion PASS."""
    manifest = [
        "completion-verifier: PASS",
        f"ticket: {ticket_id}",
        f"model: {result.get('model') or 'n/a'}",
        f"runner: {result.get('runner') or 'n/a'}",
        signing.rebar_version_step(signing.gate_code_version()),
    ]
    basis_raw = result.get("completion_read_basis")
    basis = CompletionReadBasis.from_dict(basis_raw) if isinstance(basis_raw, Mapping) else None
    material = result.get("material_fingerprint")
    if basis is not None:
        if not isinstance(material, str) or not material:
            raise CommandError(
                "Error: atomic completion PASS has no pinned material fingerprint",
                returncode=1,
            )
        manifest.extend(_basis_steps(basis))
    elif not material:
        try:
            from rebar.llm.plan_review.attest import current_material_fingerprint

            material = current_material_fingerprint(ticket_id, repo_root=repo_root)
        except Exception:  # noqa: BLE001 — legacy signatures keep best-effort material behavior
            material = None
    if material:
        manifest.append(f"material: {material}")
    sha = result.get("verified_at_sha")
    if sha:
        manifest.append(signing.verified_at_sha_step(sha))
    if result.get("disposition"):
        from rebar._commands import close_disposition

        return close_disposition.decorate_manifest(manifest, dict(result))
    return manifest


def _parse_basis(result: Mapping[str, Any]) -> CompletionReadBasis:
    raw = result.get("completion_read_basis")
    if not isinstance(raw, Mapping):
        raise CommandError("Error: completion PASS has no read basis", returncode=1)
    try:
        basis = CompletionReadBasis.from_dict(raw)
    except (TypeError, ValueError) as exc:
        raise CommandError(f"Error: invalid completion read basis: {exc}", returncode=1) from None
    verified = str(result.get("verified_at_sha") or "")
    if verified != basis.code_oid.value:
        raise CommandError(
            "Error: completion read basis code OID differs from verified_at_sha",
            returncode=1,
        )
    return basis


def _compose_prepared_bundle(
    result: dict[str, Any], ticket_id: str, repo_root
) -> PreparedCompletionBundle:
    """Mint expensive payloads and reserve UUIDs before acquiring the write lock."""
    from rebar.llm import completion_sidecar

    basis = _parse_basis(result)
    material = result.get("material_fingerprint")
    if not isinstance(material, str) or not material:
        raise CommandError(
            "Error: completion PASS cannot be atomically certified without pinned material",
            returncode=1,
        )
    payload = completion_sidecar.build_payload(result, material=material)
    manifest = verdict_manifest(result, ticket_id, repo_root)
    try:
        resolved, signature_payload = signing._prepare_manifest_event(
            ticket_id,
            manifest,
            kind="completion-verifier",
            repo_root=repo_root,
        )
    except signing.SigningError as exc:
        raise CommandError(
            f"Error: atomic completion close could not prepare its signature: {exc.message}",
            returncode=exc.returncode,
        ) from None
    if resolved != ticket_id:
        raise CommandError(
            f"Error: atomic close resolved {ticket_id!r} as unexpected ticket {resolved!r}",
            returncode=1,
        )
    return PreparedCompletionBundle(
        basis=basis,
        verdict_payload=payload,
        signature_payload=signature_payload,
        verdict_uuid=str(uuid.uuid4()),
        status_uuid=str(uuid.uuid4()),
        signature_uuid=str(uuid.uuid4()),
    )


def _matches_basis(candidate: object, basis: CompletionReadBasis) -> bool:
    if not isinstance(candidate, Mapping):
        return False
    try:
        other = CompletionReadBasis.from_dict(candidate)
    except (TypeError, ValueError):
        return False
    return (
        other.code_oid == basis.code_oid
        and other.tickets_oid == basis.tickets_oid
        and other.receipt_digest == basis.receipt_digest
    )


def _equivalent_close_at(
    tracker: str,
    ticket_id: str,
    basis: CompletionReadBasis,
    oid: TicketsOID,
    *,
    repo_root,
) -> bool:
    """Recognize the same certified basis after a same-ticket local or remote race."""
    try:
        from rebar._engine_support.reads import use_ticket_view
        from rebar.llm.plan_review.attest import compute_validity

        with PinnedTicketView.at_oid(tracker, oid) as view:
            with use_ticket_view(view):
                state = view.show_ticket(ticket_id)
                if state.get("status") != "closed":
                    return False
                sidecar = any(
                    payload.get("verdict") == "PASS"
                    and _matches_basis(payload.get("completion_read_basis"), basis)
                    for payload in view.event_payloads(ticket_id, "COMPLETION_VERDICT")
                )
                required_steps = set(_basis_steps(basis))
                attestations = state.get("attestations") or {}
                record = (
                    attestations.get("completion-verifier")
                    if isinstance(attestations, Mapping)
                    else None
                )
                certified = signing.verify_attestation_record(
                    record if isinstance(record, dict) else None,
                    ticket_id,
                    kind="completion-verifier",
                    repo_root=repo_root,
                )
                manifest = certified.get("signed_manifest")
                signed = (
                    certified.get("verdict") == "certified"
                    and isinstance(manifest, list)
                    and required_steps.issubset(set(manifest))
                    and manifest[:1] == ["completion-verifier: PASS"]
                    and compute_validity(
                        certified,
                        state,
                        "completion-verifier",
                        repo_root=repo_root,
                    ).get("valid")
                    is True
                )
                return sidecar and signed
    except Exception:  # noqa: BLE001 — unreadable equivalence is never assumed
        return False


def _receipt_conflict(validation: ReceiptValidation, ticket_id: str) -> CommandError:
    detail = ", ".join(validation.conflicts[:12]) or "unknown ticket-store drift"
    return txn_concurrency_error(
        f"Error: cannot close {ticket_id}: ticket material read by completion changed "
        f"before publication ({detail}); run completion verification again"
    )


def txn_concurrency_error(message: str) -> CommandError:
    """Construct the public exit-10 mismatch without importing transition orchestration."""
    from rebar._commands.txn import ConcurrencyMismatch

    return ConcurrencyMismatch(message)


def _validate_code_ref(basis: CompletionReadBasis, ref: str | None, repo_root) -> None:
    from rebar._snapshot.repo_snapshot import resolve_ref

    current = resolve_ref(ref or "HEAD", str(config.repo_root(repo_root)), fetch=False)
    if current != basis.code_oid.value:
        raise txn_concurrency_error(
            "Error: code ref changed after completion verification; verify and close the "
            "same --ref again"
        )


def _require_atomic_delivery_mode(repo_root) -> None:
    """Refuse a mid-run push-policy change before the local bundle is committed."""
    from rebar._store import push

    root = str(config.repo_root(repo_root))
    mode = push._push_mode(root)
    if mode != "always":
        raise txn_concurrency_error(
            "Error: sync.push changed after the pinned completion run selected atomic "
            f"delivery (now {mode!r}); retry the close so it can select a compatible "
            "ticket-read path"
        )


def commit_completion_bundle(
    result: dict[str, Any],
    ticket_id: str,
    tracker: str,
    repo_root,
    *,
    ref: str | None,
    env_id: str,
    author: str,
    close_class: str = "",
    close_reason: str = "",
    completion_expectation: str = "required",
    pre_status_check: Callable[[Mapping[str, Any]], None] | None = None,
) -> AtomicBundleResult:
    """Validate a completion receipt, atomically close, then deliver outside the lock."""
    basis = _parse_basis(result)
    _validate_code_ref(basis, ref, repo_root)
    _require_atomic_delivery_mode(repo_root)
    receipt_validation_ms = 0
    initial, elapsed = _timed_receipt_validation(tracker, basis.receipt)
    receipt_validation_ms += elapsed
    if not initial.valid:
        if _equivalent_close_at(
            tracker, ticket_id, basis, initial.current_oid, repo_root=repo_root
        ):
            return AtomicBundleResult(
                {"signed": True, "cause": "already_equivalent", "error": ""},
                {
                    "idempotent": True,
                    "commit_oid": initial.current_oid.value,
                    "receipt_digest": basis.receipt_digest,
                    "delivery": "already_present",
                    "atomic_close_prepare_ms": 0,
                    "atomic_close_receipt_validation_ms": receipt_validation_ms,
                },
            )
        raise _receipt_conflict(initial, ticket_id)
    prepare_started = time.monotonic_ns()
    prepared = _compose_prepared_bundle(result, ticket_id, repo_root)
    prepare_ms = _elapsed_ms(prepare_started)
    transaction_metrics: dict[str, int] = {}
    delivery_metrics: dict[str, int] = {}
    for attempt in range(1, _LOCAL_ATTEMPTS + 1):
        validation, elapsed = _timed_receipt_validation(tracker, basis.receipt)
        receipt_validation_ms += elapsed
        if not validation.valid:
            if _equivalent_close_at(
                tracker, ticket_id, basis, validation.current_oid, repo_root=repo_root
            ):
                return AtomicBundleResult(
                    {"signed": True, "cause": "already_equivalent", "error": ""},
                    {
                        "idempotent": True,
                        "commit_oid": validation.current_oid.value,
                        "receipt_digest": basis.receipt_digest,
                        "delivery": "already_present",
                        "atomic_close_attempts": attempt,
                        "atomic_close_prepare_ms": prepare_ms,
                        "atomic_close_receipt_validation_ms": receipt_validation_ms,
                        **transaction_metrics,
                        **delivery_metrics,
                    },
                )
            raise _receipt_conflict(validation, ticket_id)
        try:
            committed = completion_txn.commit_atomic_completion_close(
                tracker,
                ticket_id,
                expected_tickets_oid=validation.current_oid,
                verdict_payload=prepared.verdict_payload,
                signature_payload=prepared.signature_payload,
                verdict_uuid=prepared.verdict_uuid,
                status_uuid=prepared.status_uuid,
                signature_uuid=prepared.signature_uuid,
                run_id=basis.run_id,
                env_id=env_id,
                author=author,
                close_class=close_class,
                close_reason=close_reason,
                completion_expectation=completion_expectation,
                repo_root=repo_root,
                pre_status_check=pre_status_check,
            )
        except completion_txn.TrackerHeadAdvanced as exc:
            _accumulate_metrics(transaction_metrics, exc.metrics)
            continue
        _accumulate_metrics(transaction_metrics, committed.metrics)
        try:
            delivered = completion_delivery.deliver_candidate(
                committed.candidate,
                tracker,
                ticket_id,
                basis,
                repo_root,
                lambda candidate_tracker, candidate_ticket, candidate_basis, candidate_oid: (
                    _equivalent_close_at(
                        candidate_tracker,
                        candidate_ticket,
                        candidate_basis,
                        candidate_oid,
                        repo_root=repo_root,
                    )
                ),
            )
        finally:
            committed.candidate.cleanup()
        receipt_validation_ms += delivered.metrics.get(
            "atomic_close_delivery_receipt_validation_ms", 0
        )
        _accumulate_metrics(delivery_metrics, delivered.metrics)
        if delivered.retry:
            continue
        details: dict[str, object] = {
            "idempotent": delivered.idempotent,
            "commit_oid": delivered.commit_oid.value,
            "receipt_digest": basis.receipt_digest,
            "delivery": delivered.state,
            "atomic_close_attempts": attempt,
            "atomic_close_prepare_ms": prepare_ms,
            "atomic_close_receipt_validation_ms": receipt_validation_ms,
            **transaction_metrics,
            **delivery_metrics,
        }
        cause = "already_equivalent" if delivered.idempotent else "signed"
        return AtomicBundleResult({"signed": True, "cause": cause, "error": ""}, details)
    raise txn_concurrency_error(
        f"Error: ticket store advanced during {_LOCAL_ATTEMPTS} atomic close publication "
        "attempts; retry"
    )


__all__ = [
    "AtomicBundleResult",
    "commit_completion_bundle",
    "has_atomic_basis",
    "verdict_manifest",
]
