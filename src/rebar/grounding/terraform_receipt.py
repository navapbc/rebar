"""Receipt + evidence construction for Terraform structural grounding (REB-640).

The single home for (a) the closed abstention-translation table that pairs a
generic grounding ``reason`` with the receipt's closed ``reason_detail``, (b) the
credential-safe grounding-evidence records, and (c) the canonical receipt with its
content digests. Split out of :mod:`rebar.grounding.terraform_tools` along the
evidence/receipt seam so each unit stays small and loads whole.

Everything here is structural: a receipt/evidence record carries only closed
classes, ``sha256:`` digests, and repo-relative locations — never a credential, an
attribute literal, or a ``default`` value.
"""

from __future__ import annotations

from typing import Any

from . import evidence as ev

#: Backend identity (the receipt ``backend`` block, minus the config digest).
PARSER = "python-hcl2"
PARSER_VERSION = "8.1.3"
ANALYZER = "rebar-terraform-structural"
ANALYZER_VERSION = 1
CONFIG_INSPECT = "terraform-config-inspect"
CONFIG_INSPECT_ANALYZER = "rebar-terraform-config-inspect-corroborator"
SCHEMA_VERSION = 1

#: The CLOSED abstention-translation table: ``reason_detail`` (receipt) ->
#: ``(reason, reason_detail)`` where ``reason`` is the generic grounding evidence
#: reason. Keyed by the receipt detail so the two records cannot drift apart.
ABSTENTIONS: dict[str, tuple[str, str]] = {
    "missing_extra": ("no_tool", "missing_extra"),
    "parser_version": ("version_skew", "parser_version"),
    "not_terraform": ("unsupported_lang", "not_terraform"),
    "invalid_input": ("parse_error", "invalid_input"),
    "unreadable_file": ("parse_error", "unreadable_file"),
    "worker_timeout": ("timeout", "worker_timeout"),
    "worker_failure": ("other", "worker_failure"),
    "duplicate_address": ("ambiguous", "duplicate_address"),
    "no_unique_address": ("ambiguous", "no_unique_address"),
    "dynamic_source": ("ambiguous", "dynamic_source"),
    "dynamic_expression": ("ambiguous", "dynamic_expression"),
    "computed_value": ("ambiguous", "computed_value"),
    "provider_attribute": ("ambiguous", "provider_attribute"),
    "splat_index": ("ambiguous", "splat_index"),
    "unknown_tfvars": ("ambiguous", "unknown_tfvars"),
    "path_outside_snapshot": ("private_or_internal_suspected", "path_outside_snapshot"),
    "module_limit": ("other", "module_limit"),
    "file_limit": ("other", "file_limit"),
    "byte_limit": ("other", "byte_limit"),
    "registry_not_found": ("private_or_internal_suspected", "registry_not_found"),
    "registry_unauthorized": ("private_or_internal_suspected", "registry_unauthorized"),
    "registry_rate_limited": ("rate_limited", "registry_rate_limited"),
    "dns_error": ("network_error", "dns_error"),
    "tls_error": ("network_error", "tls_error"),
    "proxy_error": ("network_error", "proxy_error"),
    "registry_server_error": ("network_error", "registry_server_error"),
    "rejected_redirect": ("network_error", "rejected_redirect"),
    "probe_timeout": ("timeout", "probe_timeout"),
    "malformed_metadata": ("parse_error", "malformed_metadata"),
    "oversized_metadata": ("parse_error", "oversized_metadata"),
    "rejected_target": ("ambiguous", "rejected_target"),
    "non_registry_remote": ("ambiguous", "non_registry_remote"),
    "invalid_detector": ("invalid_detector", "invalid_detector"),
    "executable_not_resolvable": ("no_tool", "executable_not_resolvable"),
    "rejected_executable": ("invalid_detector", "rejected_executable"),
    "binary_replaced": ("invalid_detector", "binary_replaced"),
    "nonzero_exit": ("other", "nonzero_exit"),
    "upstream_diagnostics": ("parse_error", "upstream_diagnostics"),
    "config_inspect_schema_skew": ("version_skew", "config_inspect_schema_skew"),
}

PROBE_REASON_DETAILS: tuple[str, ...] = (
    "registry_not_found",
    "registry_unauthorized",
    "registry_rate_limited",
    "dns_error",
    "tls_error",
    "proxy_error",
    "registry_server_error",
    "rejected_redirect",
    "probe_timeout",
    "malformed_metadata",
    "oversized_metadata",
    "rejected_target",
    "non_registry_remote",
)

#: Worker fail-open ``abstain_reason`` (from the harness) -> receipt reason_detail.
WORKER_REASON_DETAIL: dict[str, str] = {
    "timeout": "worker_timeout",
    "parse_error": "invalid_input",
    "version_skew": "parser_version",
    "other": "worker_failure",
}


def _content_digest(obj: Any) -> str:
    from rebar._store.canonical import content_hash

    return "sha256:" + content_hash(obj)


def config_digest() -> str:
    """A stable ``sha256:`` digest of the backend/analyzer configuration."""
    return _content_digest(
        {
            "parser": PARSER,
            "parser_version": PARSER_VERSION,
            "analyzer": ANALYZER,
            "analyzer_version": ANALYZER_VERSION,
        }
    )


def backend_block() -> dict[str, Any]:
    """The receipt ``backend`` block (parser/analyzer identity + config digest)."""
    return {
        "parser": PARSER,
        "parser_version": PARSER_VERSION,
        "analyzer": ANALYZER,
        "analyzer_version": ANALYZER_VERSION,
        "config_digest": config_digest(),
    }


def config_inspect_backend_block() -> dict[str, Any]:
    return {
        "parser": CONFIG_INSPECT,
        "parser_version": "unknown",
        "analyzer": CONFIG_INSPECT_ANALYZER,
        "analyzer_version": ANALYZER_VERSION,
        "config_digest": _content_digest(
            {
                "parser": CONFIG_INSPECT,
                "parser_version": "unknown",
                "analyzer": CONFIG_INSPECT_ANALYZER,
                "analyzer_version": ANALYZER_VERSION,
            }
        ),
    }


def coverage_ran() -> dict[str, Any]:
    return ev.coverage(backend=PARSER, status="ran", version=PARSER_VERSION)


def config_inspect_coverage_ran() -> dict[str, Any]:
    return ev.coverage(backend=CONFIG_INSPECT, status="ran", version="unknown")


def refuted_evidence(
    reference: dict[str, Any],
    location: dict[str, Any] | None,
    *,
    tier: str = ev.TIER_T1,
    detail: str | None = None,
) -> dict[str, Any]:
    """A ``refuted`` grounding-evidence record (a real declaration disproves absence)."""
    record = ev.refuted(
        provenance_tier=tier,
        coverage=coverage_ran(),
        reference=reference,
        location=location,
        detail=detail,
    )
    record["reason"] = None
    return record


def abstain_evidence(
    reason: str,
    reference: dict[str, Any] | None = None,
    *,
    detail: str | None = None,
    job: str = ev.JOB_REFUTE,
    backend: str = PARSER,
    version: str = PARSER_VERSION,
) -> dict[str, Any]:
    """An ``abstain`` grounding-evidence record with a generic closed ``reason``."""
    return ev.abstain(
        reason,
        job=job,
        provenance_tier=ev.TIER_T1,
        backend=backend,
        version=version,
        reference=reference,
        detail=detail,
    )


def match_evidence(
    location: dict[str, Any] | None,
    *,
    detail: str | None = None,
) -> dict[str, Any]:
    return ev.match(
        job=ev.JOB_APPLIES,
        provenance_tier=ev.TIER_T1,
        coverage=config_inspect_coverage_ran(),
        location=location,
        detail=detail,
    )


def _refuted_result_digest(reference: dict[str, Any], location: dict[str, Any] | None) -> str:
    return _content_digest({"outcome": "refuted", "reference": reference, "location": location})


def _abstain_result_digest(reason: str, reason_detail: str) -> str:
    return _content_digest({"outcome": "abstain", "reason": reason, "reason_detail": reason_detail})


def _match_result_digest(
    subject: dict[str, Any], location: dict[str, Any] | None, executable_hash: str
) -> str:
    return _content_digest(
        {
            "outcome": "match",
            "subject": subject,
            "location": location,
            "executable_hash": executable_hash,
        }
    )


def _receipt_common(
    operation: str,
    query: dict[str, Any],
    snapshot_digest: str,
    module_digest: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": operation,
        "query": query,
        "snapshot_digest": snapshot_digest,
        "module_digest": module_digest,
        "backend": backend_block(),
        "limits": _limits(),
    }


def _receipt_common_with_backend(
    operation: str,
    query: dict[str, Any],
    snapshot_digest: str,
    module_digest: str,
    backend: dict[str, Any],
) -> dict[str, Any]:
    receipt = _receipt_common(operation, query, snapshot_digest, module_digest)
    receipt["backend"] = backend
    return receipt


def _limits() -> dict[str, int]:
    from . import terraform_index as tfi

    return dict(tfi.LIMITS)


def refuted_receipt(
    operation: str,
    query: dict[str, Any],
    snapshot_digest: str,
    module_digest: str,
    reference: dict[str, Any],
    location: dict[str, Any] | None,
    *,
    source_kind: str | None = None,
) -> dict[str, Any]:
    """The canonical receipt for a ``refuted`` outcome (hashes the safe facts)."""
    receipt = _receipt_common(operation, query, snapshot_digest, module_digest)
    receipt.update(
        outcome="refuted",
        reason=None,
        reason_detail=None,
        result_digest=_refuted_result_digest(reference, location),
    )
    if source_kind is not None:
        receipt["source_kind"] = source_kind
    return receipt


def abstain_receipt(
    operation: str,
    query: dict[str, Any],
    snapshot_digest: str,
    module_digest: str,
    reason_detail: str,
    *,
    backend: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The canonical receipt for an ``abstain`` outcome.

    ``reason_detail`` keys the closed :data:`ABSTENTIONS` table; the abstain
    ``result_digest`` hashes EXACTLY ``{outcome, reason, reason_detail}``.
    """
    reason, detail = ABSTENTIONS[reason_detail]
    if backend is None:
        receipt = _receipt_common(operation, query, snapshot_digest, module_digest)
    else:
        receipt = _receipt_common_with_backend(
            operation, query, snapshot_digest, module_digest, backend
        )
    receipt.update(
        outcome="abstain",
        reason=reason,
        reason_detail=detail,
        result_digest=_abstain_result_digest(reason, detail),
    )
    return receipt


def match_receipt(
    operation: str,
    query: dict[str, Any],
    snapshot_digest: str,
    module_digest: str,
    subject: dict[str, Any],
    location: dict[str, Any] | None,
    executable: dict[str, Any],
    invocation: dict[str, Any],
) -> dict[str, Any]:
    receipt = _receipt_common_with_backend(
        operation, query, snapshot_digest, module_digest, config_inspect_backend_block()
    )
    receipt.update(
        outcome="match",
        reason=None,
        reason_detail=None,
        result_digest=_match_result_digest(subject, location, executable["sha256"]),
        executable=executable,
        invocation=invocation,
    )
    return receipt
