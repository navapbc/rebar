"""Interface presentation filter for compiled ticket state.

The reducer keeps internal bookkeeping fields in its in-process state — notably
``parent_status_uuid`` and ``last_status_env_id`` (optimistic-concurrency / fork
resolution markers used by transition/claim and compaction). Those must stay in
the reducer for internal consumers, but they are NOT part of the public output
contract and must not appear in ``show`` / ``list`` / ``search`` / ``ready`` /
``--output llm`` output.

Historically the jq ``show`` reducer omitted these keys while the Python reducer
emitted them, which is exactly the show-vs-list shape divergence this filter (plus
the single-reducer refactor) resolves: every interface now reduces via the Python
reducer and strips internal keys here, so all interfaces return one shape.
"""

from __future__ import annotations

from typing import Any

# Top-level reducer keys that are internal-only and must be hidden from the
# public interface contract.
INTERNAL_KEYS: frozenset[str] = frozenset(
    {
        "parent_status_uuid",
        "last_status_env_id",
    }
)

# Internal sub-keys to strip from nested objects.
_INTERNAL_SUBKEYS: dict[str, frozenset[str]] = {
    # source_count is the count of folded precondition sources — internal detail.
    "preconditions_summary": frozenset({"source_count"}),
    # The raw HMAC hex is the secret-ish artifact clients never need: they want the
    # FACT of a signature (the verified-steps manifest + env key fingerprint) and
    # its validity via `verify-signature` / fsck / validate — not the signature
    # itself. The reducer keeps the full record (the close gate and verify read it
    # directly); only the public interface output drops the hex.
    "signature": frozenset({"signature"}),
}


def public_state(state: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a compiled ticket state with internal-only keys removed.

    Pure / non-mutating: the caller's reducer state is left intact (internal
    consumers still see the full state). Applied at every interface output site.
    """
    out = {k: v for k, v in state.items() if k not in INTERNAL_KEYS}
    for parent_key, subkeys in _INTERNAL_SUBKEYS.items():
        nested = out.get(parent_key)
        if isinstance(nested, dict) and any(s in nested for s in subkeys):
            out[parent_key] = {k: v for k, v in nested.items() if k not in subkeys}
    # `attestations` is a kind-keyed map of signature records (epic dark-acme-lumen); the
    # flat `_INTERNAL_SUBKEYS` rule above strips one level (the legacy single `signature`),
    # but the HMAC hex here lives TWO levels deep (`attestations.<kind>.signature`). Strip it
    # from EVERY per-kind record — the canonical hex-strip path for all present + future
    # kinds, so the raw signature never leaks into show/list/search/MCP output.
    attestations = out.get("attestations")
    if isinstance(attestations, dict):
        out["attestations"] = {kind: _strip_hex(rec) for kind, rec in attestations.items()}
    return out


def _strip_hex(record: Any) -> Any:
    """Drop the raw HMAC hex (`signature`) from one attestation record; pass non-dicts through."""
    if not isinstance(record, dict):
        return record
    return {k: v for k, v in record.items() if k != "signature"}
