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

import hashlib
import json
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
    previous = entry.get("baseline") or {}
    entry["baseline"] = baseline
    _clear_rich_reemit_on_body_refresh(entry, previous, baseline)


def merge_baseline(bindings: dict[str, Any], local_id: str, fields: dict[str, Any]) -> None:
    """Overlay the values rebar itself just SYNCED onto a binding's existing baseline.

    The last-synced/last-fetched distinction (bug e6e9). ADR 0026 §22-42 defines the
    baseline as the LAST-SYNCED value, but ``reconcile._advance_baselines`` writes the
    pass-START snapshot, which is fetched BEFORE the outbound apply. For any field rebar
    pushed in that pass, the resulting baseline holds the value Jira had *before* our own
    write. ``local == baseline`` — ADR 0026's sole direction signal — is then FALSE for one
    pass, and a local REVERT to the pre-push value lands exactly in that window: outbound
    stands down believing local never changed, and the inbound differ mirrors Jira's
    now-stale value back over the revert.

    This applies the correction: ``set_baseline`` records the fetch, then this overlays the
    fields we actually landed. Unlike ``set_baseline`` it is a per-field MERGE, not a
    whole-dict replace — the pushed fields are the only ones we have fresher evidence for,
    and an untouched field must keep its fetched value.

    The caller MUST pass only fields whose write is CONFIRMED landed (per-mutation success,
    not "the pass ran"). A transition can soft-fail while the pass still exits 0
    (``applier._apply_one``'s backstop), and a baseline advanced for a push that never
    landed asserts a sync that did not happen. That failure does NOT self-correct, whereas
    the bug being fixed here does (after a clobber the baseline advances to Jira's value,
    local then differs, and the next pass re-pushes). Lagging is recoverable; leading is not.

    A no-op for an unbound local id or an empty overlay, and — like ``set_baseline`` — it
    leaves ``updated_at`` churn-free when nothing actually changes.
    """
    entry = bindings.get(local_id)
    if entry is None:
        return
    overlay = {k: normalize_baseline_value(k, fields[k]) for k in _BASELINE_FIELDS if k in fields}
    if not overlay:
        return
    baseline = dict(entry.get("baseline") or {})
    merged = {**baseline, **overlay}
    if merged == baseline:
        return
    entry["baseline"] = merged
    _clear_rich_reemit_on_body_refresh(entry, baseline, merged)


# -- rich-text emit state (story 3388) ---------------------------------------
#
# Two fields stored INLINE on the binding beside ``baseline``: ``rich_sha`` (the
# digest of the description wire rebar last pushed) and ``rich_reemit`` (how many
# times in a row that identical wire has been re-pushed). They exist because the
# DC codec is one-way and lossy, so a body is not guaranteed to reach a codec
# fixed point; the pair bounds that tail instead of assuming it away.
#
# Both obey epic 0303's churn discipline: fixed size, change-gated, never a
# per-pass timestamp. A pass that pushes no description writes neither field, and
# a converging body never stores ``rich_reemit`` at all (absent means zero).

RICH_REEMIT_OBSERVE_AT = 2
"""Re-emit count at which the caller should OBSERVE the body Jira actually stored.

Two means the same wire has been pushed three times (0, 1, 2) without the
baseline moving underneath it — no longer explicable as one pass of lag, so the
next step is evidence rather than another blind re-push. It is an equality
threshold, not a floor, so the observation costs exactly one GET per divergence
episode however long the episode lasts.
"""


def rich_sha(wire: Any) -> str:
    """A change-gate digest of a rendered rich-text wire: 16 hex chars = 8 bytes.

    Shape-tolerant because the two clients' wires differ in kind — Cloud sends an
    ADF *dict*, Data Center a wiki *string* — so a non-string is serialized with
    sorted keys first, making the digest independent of dict ordering.

    Truncation is deliberate. This gates a re-emit; it authenticates nothing, and
    the field is carried on every binding in every committed version of the
    store, so its SIZE is the design constraint (0303).
    """
    payload = wire if isinstance(wire, str) else json.dumps(wire, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def note_rich_emit(entry: dict[str, Any], wire: Any) -> int:
    """Record that ``wire`` was CONFIRMEDLY pushed as this binding's description.

    Returns how many times in a row the SAME wire has now been pushed: 0 the first
    time a given wire goes out, 1 the next time, and so on. A body that converges
    is pushed once and then never again, so the count never leaves 0 and
    ``rich_reemit`` is never written — which is what keeps a no-op pass at zero
    changed entries.

    Change-gated in both directions: ``rich_sha`` is written only when the wire
    actually differs from the last one, and a differing wire RESETS the counter,
    so a genuinely edited body never inherits the previous body's re-emit history.

    ``entry`` is the LIVE binding record — ``BindingStore.all_bindings()`` copies
    only the outer mapping, so mutating an entry in place is persisted by the
    store's own ``save()``. That is the same no-new-commit-surface property
    ``set_baseline`` relies on (ADR 0026 §Consequences), and it is why this takes
    an entry rather than the bindings mapping: the caller already holds the record
    it means to annotate, and cannot annotate one that does not exist.
    """
    sha = rich_sha(wire)
    if entry.get("rich_sha") != sha:
        entry["rich_sha"] = sha
        entry.pop("rich_reemit", None)
        return 0
    count = int(entry.get("rich_reemit") or 0) + 1
    entry["rich_reemit"] = count
    return count


def _clear_rich_reemit_on_body_refresh(
    entry: dict[str, Any], previous: dict[str, Any], updated: dict[str, Any]
) -> None:
    """End a re-emit episode when the baseline's BODY moves.

    The counter is waiting for exactly one thing: fresh evidence of what Jira
    stores for this description. A baseline whose description has changed IS that
    evidence, whatever produced it — the pass-start fetch or the observed-after-
    push overlay — so the episode is over and the count starts again from zero.

    Gated on the description alone. A baseline write driven by some other field
    (status, assignee) says nothing about the body, and clearing on it would let a
    body loop indefinitely while an unrelated field churned beside it.

    ``rich_sha`` is deliberately left alone: it records the last wire we SENT,
    which a baseline refresh does not change.
    """
    if previous.get("description") != updated.get("description"):
        entry.pop("rich_reemit", None)


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
