"""Last-synced PEER STATE recorded per binding (ADR 0026 + ticket 88d9).

Answers one question for the reconciler's arbitration passes: "what did the
peer look like the last time we successfully looked". Two channels live here:

1. **Baseline** — the ADR-0026 three-way-merge ancestor: the five last-synced
   Jira-side inbound-mirrored field values (``_BASELINE_FIELDS``). An ABSENT
   baseline (including any version-1 store or pre-baseline entry) is VALID and
   degrades to local-wins (ADR 0026 §2).
2. **Peer parent** — the last-OBSERVED peer parent key (ticket 88d9), the
   evidence channel for an inbound parent CLEAR. An ABSENT observation is
   VALID and MUST fail safe to NO clear.

These are free functions over the binding store's ``bindings`` dict (the
``self._data["bindings"]`` mapping) rather than methods, extracted from
``binding_store.py`` along the existing call-graph seam (ticket 4522):
``BindingStore`` keeps thin delegating methods so its public surface is
unchanged, and the peer-state semantics are directly unit-testable without
constructing a store. State is mutated in-memory only — persistence stays
with the existing binding-store ``save()`` commit path (no new commit
surface, ADR 0026 §Consequences).

Consumers (via the ``BindingStore`` delegates): ``outbound_field_diff``,
``inbound_differ``, ``binding_walk``, ``classify``,
``adapters/jira/outbound_fields``, ``apply_inbound_records``, and the
advance sites ``reconcile._advance_baselines`` / ``_advance_peer_parent``.
None of them touch binding lifecycle.
"""

from __future__ import annotations

from typing import Any

from rebar_reconciler.inbound_fields import normalize_baseline_value

# ADR 0026: the five last-synced Jira-side inbound-mirrored fields per binding.
# An absent baseline (including v1) is valid and degrades to local-wins.
_BASELINE_FIELDS: tuple[str, ...] = (
    "summary",
    "description",
    "priority",
    "status",
    "assignee",
)


def get_baseline(bindings: dict[str, Any], local_id: str) -> dict[str, Any] | None:
    """Return the last-synced Jira-side field values for a binding, or None.

    None (an absent baseline) is VALID and means "no last-synced ancestor
    yet" — the consumer degrades to local-wins (ADR 0026 §2). A version-1
    store, or an entry that predates baselines, simply has no ``baseline``
    key and returns None here.
    """
    entry = bindings.get(local_id)
    if entry is None:
        return None
    baseline = entry.get("baseline")
    if not isinstance(baseline, dict):
        return None
    return dict(baseline)


def set_baseline(bindings: dict[str, Any], local_id: str, fields: dict[str, Any]) -> None:
    """Record the last-synced Jira-side values for a binding's 5 mirrored fields.

    Filters ``fields`` to ``_BASELINE_FIELDS`` (so a whole prev_snapshot entry
    can be passed directly). A no-op if the local id is not bound (you cannot
    baseline an unbound pair). In-memory until
    ``save()`` — persisted by the existing binding-store commit path, no new
    commit surface (ADR 0026 §Consequences).
    """
    entry = bindings.get(local_id)
    if entry is None:
        return
    baseline = {k: normalize_baseline_value(k, fields[k]) for k in _BASELINE_FIELDS if k in fields}
    if entry.get("baseline") == baseline:
        return
    entry["baseline"] = baseline


def get_peer_parent(bindings: dict[str, Any], local_id: str) -> str | None:
    """The peer parent key rebar last OBSERVED for this binding, or None.

    The evidence channel for an inbound parent CLEAR — it answers "did the peer ever have
    a parent", which ``managed_refs`` cannot (``add_managed_ref`` fires on the LOCAL
    parent-set event, so managed never meant pushed; reading the peer's silence as a
    deletion orphaned 63 tickets, ticket 88d9). None is VALID and MUST fail safe to no
    clear: a v1 store, a pre-field binding, an unconfirmed binding and an out-of-window
    key all present as None.
    """
    entry = bindings.get(local_id)
    if entry is None:
        return None
    value = entry.get("peer_parent")
    return value if isinstance(value, str) and value else None


def set_peer_parent(bindings: dict[str, Any], local_id: str, parent_key: str | None) -> None:
    """Record the peer parent key OBSERVED this pass (None = observed to have none).

    No-op for an unbound id; in-memory until ``save()``, persisted by the existing
    binding-store commit path (no new commit surface, mirroring ``set_baseline``).

    **Callers MUST NOT call this for a pass that did not OBSERVE the parent field.** A
    fail-open read (``get_parent_map`` degrades to ``{}``; a truncated page walk omits
    issues) would overwrite a good observation with "no parent", and the next pass would
    read that as a deletion — the orphaning incident by a longer route. Only the caller can
    see whether the snapshot entry carried the key, so only the caller can decide.
    """
    entry = bindings.get(local_id)
    if entry is None:
        return
    normalized = parent_key if isinstance(parent_key, str) and parent_key else ""
    if entry.get("peer_parent", None) == normalized:
        return
    entry["peer_parent"] = normalized


def seed_baselines_from_snapshot(bindings: dict[str, Any], prev_snapshot: dict[str, Any]) -> int:
    """One-shot: seed a baseline for every bound key present in a Jira snapshot.

    ``prev_snapshot`` is ``{jira_key: {summary, description, priority, status,
    assignee, ...}}``. Derisk X4 proved all 613 bound+present keys carry all 5
    mirrored fields, so already-bound pairs need no cold-start local-wins window.
    Does NOT delete prev_snapshot or change its consumers (that is the rollout
    task's swap). Returns the number of baselines seeded.
    """
    seeded = 0
    for local_id, entry in bindings.items():
        jira_key = entry.get("jira_key")
        if jira_key and jira_key in prev_snapshot:
            set_baseline(bindings, local_id, prev_snapshot[jira_key])
            seeded += 1
    return seeded
