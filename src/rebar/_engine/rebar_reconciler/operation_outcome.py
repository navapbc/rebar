"""Provider-neutral logical-operation outcome values, bounded diagnostics, and
canonical serialization.

This is a *leaf* value module for the reconciler mutate path (ticket
7bc2-5203-d5f4-4a4a). It carries no I/O and no provider coupling: it defines the
four provider-neutral enums, an allowlisting diagnostic bounder, and a frozen
``OperationOutcome`` whose canonical bytes are produced *only* through the store
seam so equivalent outcomes serialize byte-identically.

Import convention: this package ships as package DATA under ``src/rebar/_engine``
and is not importable as ``rebar._engine.rebar_reconciler``. The main ``rebar``
package is importable normally, hence the two absolute seam imports below.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from rebar._store.canonical import canonical_bytes as _store_canonical_bytes
from rebar.llm.failure import sanitize_diagnostic


class Disposition(str, enum.Enum):
    applied = "applied"
    already_satisfied = "already_satisfied"
    recovered = "recovered"
    retryable_deferred = "retryable_deferred"
    commit_unknown = "commit_unknown"
    permanent_failure = "permanent_failure"
    exhausted_transient = "exhausted_transient"
    dependency_deferred = "dependency_deferred"
    scope_deferred = "scope_deferred"
    safety_aborted = "safety_aborted"
    skipped = "skipped"


# ``global`` is a Python keyword, so FailureScope uses the functional form to let
# the member NAMED and VALUED "global" exist.
FailureScope = enum.Enum(
    "FailureScope",
    {
        "none": "none",
        "ticket": "ticket",
        "endpoint": "endpoint",
        "tenant": "tenant",
        "provider": "provider",
        "global": "global",
    },
    type=str,
)


class ReplaySafety(str, enum.Enum):
    not_applicable = "not_applicable"
    safe = "safe"
    observe_first = "observe_first"
    forbidden = "forbidden"


class DelaySource(str, enum.Enum):
    none = "none"
    fallback_jitter = "fallback_jitter"
    provider = "provider"
    fuse = "fuse"


_DIAGNOSTIC_KEYS: tuple[str, ...] = (
    "stage",
    "category",
    "status_code",
    "provider_code",
    "retry_after_ms",
    "message",
)

_MAX_DIAGNOSTICS = 8
_MAX_MESSAGE_CODEPOINTS = 512


def _redact_scalar(value: object) -> object:
    """Route a diagnostic value through the ADR 0041 redaction seam. A string is
    stringified (a no-op) and passed through the secret redactor; a non-string carrying a
    secret in its repr is coerced to text first so it cannot bypass redaction; a genuine
    non-string scalar with no secret (an int code) is returned unchanged."""
    if isinstance(value, str):
        return sanitize_diagnostic({"message": value})["message"]
    coerced = str(value)
    redacted = sanitize_diagnostic({"message": coerced})["message"]
    return value if redacted == coerced else redacted


def _redact_message(message: object) -> str:
    text = sanitize_diagnostic({"message": str(message)})["message"]
    if len(text) > _MAX_MESSAGE_CODEPOINTS:
        return text[: _MAX_MESSAGE_CODEPOINTS - 1] + "\u2026"
    return text


def _bound_one(raw: Mapping[str, object]) -> Mapping[str, object]:
    kept: dict[str, object] = {}
    for key in _DIAGNOSTIC_KEYS:
        if key not in raw:
            continue
        if key == "message":
            kept[key] = _redact_message(raw[key])
        else:
            kept[key] = _redact_scalar(raw[key])
    return MappingProxyType(kept)


def bound_diagnostics(
    raw_entries: object,
) -> tuple[Mapping[str, object], ...]:
    entries = list(raw_entries)  # type: ignore[call-overload]
    total = len(entries)
    if total <= _MAX_DIAGNOSTICS:
        return tuple(_bound_one(entry) for entry in entries)
    kept = [_bound_one(entry) for entry in entries[: _MAX_DIAGNOSTICS - 1]]
    sentinel = MappingProxyType(
        {
            "stage": "diagnostic",
            "category": "truncated",
            "message": f"dropped={total - (_MAX_DIAGNOSTICS - 1)}",
        }
    )
    kept.append(sentinel)
    return tuple(kept)


@dataclass(frozen=True)
class OperationOutcome:
    logical_id: str
    disposition: Disposition
    failure_scope: FailureScope
    replay_safety: ReplaySafety
    invocation_count: int
    request_count: int
    delay_source: DelaySource
    provider_delay_ms: int | None
    retry_not_before: str | None
    diagnostics: tuple[Mapping[str, object], ...]

    def to_canonical_dict(self) -> dict:
        doc: dict = {
            "logical_id": self.logical_id,
            "disposition": self.disposition.value,
            "failure_scope": self.failure_scope.value,
            "replay_safety": self.replay_safety.value,
            "invocation_count": self.invocation_count,
            "request_count": self.request_count,
            "delay_source": self.delay_source.value,
            "diagnostics": [dict(entry) for entry in self.diagnostics],
        }
        if self.provider_delay_ms is not None:
            doc["provider_delay_ms"] = self.provider_delay_ms
        if self.retry_not_before is not None:
            doc["retry_not_before"] = self.retry_not_before
        return doc

    def canonical_bytes(self) -> bytes:
        return _store_canonical_bytes(self.to_canonical_dict())
