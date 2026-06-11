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
    return out
