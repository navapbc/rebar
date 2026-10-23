"""Pure differ that emits deterministic Mutation objects from local and Jira state.

The production run_differs caller still supplies previous and current Jira
snapshots. Its local-state-dependent emissions are suppressed at that call site.
Callers supplying local source state retain this differ's full contract. Fields
in EXCLUDED_FIELDS never enter mutation payloads."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from dataclasses import dataclass
from types import ModuleType
from typing import Any


def _load_sibling(module_name: str, file_name: str) -> ModuleType:
    """Import the in-package sibling ``rebar.reconciler.<module_name>``."""
    del file_name
    return importlib.import_module(f"rebar.reconciler.{module_name}")


def _load_config() -> ModuleType:
    return _load_sibling("config", "config.py")


def _load_conflict_resolver() -> ModuleType:
    return _load_sibling("conflict_resolver", "conflict_resolver.py")


_MUTATION_KEY = "rebar.reconciler.mutation"


def _load_mutation() -> ModuleType:
    # Prefer an already-loaded mutation module to preserve class identity
    # across callers. Test fixtures load mutation.py under the bare ``mutation``
    # key (and historically under other private keys); production code loads
    # under the canonical ``rebar.reconciler.mutation`` key.
    # Check the test-friendly keys FIRST so a test rig that pre-seeded its own
    # module wins identity ties — otherwise tests that compare against their
    # own freshly loaded ``Mutation`` class fail with cross-identity errors.
    #
    # When no cache hit exists, load under the canonical key so future cross-
    # module lookups (invariants.py, applier.py) share the SAME module object.
    for cache_key in (
        "mutation",
        "rebar.reconciler.mutation",
        "reconciler_mutation",
        _MUTATION_KEY,
    ):
        cached = sys.modules.get(cache_key)
        if cached is not None and hasattr(cached, "Mutation"):
            return cached
    mod = importlib.import_module("rebar.reconciler.mutation")
    return mod


# Exclude identity, status, and import labels written by the bridge itself.
# Otherwise their next-pass readback appears as Jira drift and produces an echo.
_BRIDGE_INTERNAL_LABEL_PREFIXES: tuple[str, ...] = (
    "rebar-id:",
    "rebar-id-",
    "imported:",
    "rebar-status:",
    "rebar-type:",
)


def _non_internal_labels(value: Any) -> set[str]:
    """The label set minus bridge-internal entries (non-list values → empty)."""
    if not isinstance(value, (list, tuple)):
        return set()
    return {
        v for v in value if isinstance(v, str) and not v.startswith(_BRIDGE_INTERNAL_LABEL_PREFIXES)
    }


def _derive_provenance(
    target: str,
    primary_fields: dict[str, Any] | None,
    fallback_fields: dict[str, Any] | None,
    reason: str,
) -> dict[str, Any]:
    """Build a non-empty provenance Mapping for a Mutation.

    Always carries ``source`` and ``reason``. Adds ``local_id`` derived from
    the snapshot's ``local_id`` (with fallback) so downstream applier
    paths (JQL dedup, mapping.json lookup) have a non-empty key. The
    jira_key is used as a last-resort fallback — fixes F2 where empty
    local_id collapsed dedup and corrupted mapping.json.
    """
    local_id: str | None = None
    if isinstance(primary_fields, dict):
        cand = primary_fields.get("local_id")
        if cand:
            local_id = str(cand)
    if local_id is None and isinstance(fallback_fields, dict):
        cand = fallback_fields.get("local_id")
        if cand:
            local_id = str(cand)
    if local_id is None:
        local_id = target
    return {"source": "differ", "reason": reason, "local_id": local_id}


def _emit(
    mutation: Any,
    *,
    quarantine_set: set[str] | None,
    mutations_out: list[Any],
    ledger: Any | None = None,
) -> None:
    """Central emit gate for all Mutation appends in compute_mutations().

    Suppresses any mutation whose ``target`` is in ``quarantine_set``. When
    ``quarantine_set`` is None, every mutation is appended unconditionally.

    When a ``ProvenanceLedger`` is provided, additionally suppresses any
    mutation whose target+payload content-hash matches the ledger's last
    recorded value for that target (echo suppression — the same value just
    came back from the other side). Otherwise records the emitted mutation
    so subsequent passes can detect echoes.

    All Mutation-emit sites in :func:`compute_mutations` MUST route through
    this helper so the suppression policies are enforced in exactly one place.
    """
    if quarantine_set is not None and mutation.target in quarantine_set:
        return
    if ledger is not None:
        try:
            if ledger.is_echo(mutation.target, mutation.payload):
                return
        except (AttributeError, TypeError, ValueError):
            # Ledger failures (missing method, unhashable payload, bad side)
            # must not break diff emission; fall through and continue.
            pass
        # Record the about-to-be-emitted mutation on the appropriate side.
        try:
            direction_val = getattr(mutation.direction, "value", mutation.direction)
            side = "local" if "outbound" in str(direction_val) else "jira"
            ledger.record(mutation.target, side, mutation.payload)
        except (AttributeError, TypeError, ValueError):
            # Same rationale as the is_echo() guard above — record() failure
            # is non-fatal; the mutation still emits, just without provenance.
            pass
    mutations_out.append(mutation)


@dataclass
class _DiffState:
    """Derived indexes + loaded classes threaded to the per-key emit phases of
    :func:`compute_mutations` — a mechanical bundle of what were locals in the
    former single-function body (no behavior change)."""

    config: Any
    conflict_resolver: Any
    Mutation: Any
    MutationAction: Any
    MutationDirection: Any
    excluded: set
    bound_local_ids: set
    jira_local_id_to_key: dict
    local_id_to_keys: dict
    duplicate_local_ids: set
    local_rebar_ids: set
    mutations: list
    all_keys: set


def _compute_mutations_build_state(local_state, jira_state, seed_mutations) -> _DiffState:
    """Setup phase: load config/resolver/mutation classes and build the derived
    lookup indexes (bound ids, duplicate-local-id collisions, seed mutations, all_keys)."""
    config = _load_config()
    conflict_resolver = _load_conflict_resolver()
    mutation_mod = _load_mutation()
    Mutation = mutation_mod.Mutation
    MutationAction = mutation_mod.MutationAction
    MutationDirection = mutation_mod.MutationDirection

    excluded = set(config.EXCLUDED_FIELDS)

    # Build the set of local_ids already bound in the Jira working set.
    # An outbound create for a local ticket whose local_id is already
    # bound in Jira would re-create an already-mirrored issue (dd-4 AC).
    bound_local_ids: set[str] = set()
    # Reverse map: local_id -> jira_key, so we can detect dangling
    # references where the Jira side claims a binding to a local ticket
    # that no longer exists locally (dd-5).
    jira_local_id_to_key: dict[str, str] = {}
    for jira_key, jira_entry in jira_state.items():
        if isinstance(jira_entry, dict):
            cand = jira_entry.get("local_id")
            if cand:
                bound_local_ids.add(str(cand))
                jira_local_id_to_key.setdefault(str(cand), jira_key)

    # Build a map: local_id -> [local_key, ...] from local_state, to
    # detect duplicate local_id collisions across local tickets (dd-5).
    local_id_to_keys: dict[str, list[str]] = {}
    for local_key, local_entry in local_state.items():
        if isinstance(local_entry, dict):
            cand = local_entry.get("local_id")
            if cand:
                local_id_to_keys.setdefault(str(cand), []).append(local_key)
    # The set of local local_ids that have a collision (>1 owners).
    duplicate_local_ids: set[str] = {lid for lid, keys in local_id_to_keys.items() if len(keys) > 1}
    # The set of local local_ids that are present in local_state at all,
    # used to short-circuit dangling-jira-ref detection.
    local_rebar_ids: set[str] = set(local_id_to_keys.keys())

    # Seed mutations (if any) are prepended to the result list before the
    # differ walks local/jira state. They are NOT filtered through
    # quarantine_set — the caller (e.g. invariants.check_dual_identity_complete)
    # has authority over what it injects.
    mutations: list[Any] = list(seed_mutations) if seed_mutations else []
    all_keys = set(local_state) | set(jira_state)
    return _DiffState(
        config=config,
        conflict_resolver=conflict_resolver,
        Mutation=Mutation,
        MutationAction=MutationAction,
        MutationDirection=MutationDirection,
        excluded=excluded,
        bound_local_ids=bound_local_ids,
        jira_local_id_to_key=jira_local_id_to_key,
        local_id_to_keys=local_id_to_keys,
        duplicate_local_ids=duplicate_local_ids,
        local_rebar_ids=local_rebar_ids,
        mutations=mutations,
        all_keys=all_keys,
    )


def compute_mutations(
    local_state: dict[str, dict] | None = None,
    jira_state: dict[str, dict] | None = None,
    *,
    quarantine_set: set[str] | None = None,
    seed_mutations: list[Any] | None = None,
    ledger: Any | None = None,
) -> list[Any]:
    """Return deterministic mutations for local and Jira state.

    Local-only keys create unless already bound in Jira. Jira-only keys create
    inbound unless binding markers assign them to the binding-aware differs. Shared
    keys emit changed non-excluded fields. quarantine_set suppresses differ emits,
    while seed_mutations precede them without filtering. Returned mutations carry
    provenance and are sorted by target after seeds."""
    if local_state is None:
        local_state = {}
    if jira_state is None:
        jira_state = {}
    state = _compute_mutations_build_state(local_state, jira_state, seed_mutations)
    for key in sorted(state.all_keys):
        in_local = key in local_state
        in_jira = key in jira_state
        if in_local and not in_jira:
            _compute_mutations_emit_local_only(
                key, local_state, jira_state, state, quarantine_set=quarantine_set, ledger=ledger
            )
        elif in_jira and not in_local:
            _compute_mutations_emit_jira_only(
                key, jira_state, state, quarantine_set=quarantine_set, ledger=ledger
            )
        else:
            _compute_mutations_emit_both(
                key, local_state, jira_state, state, quarantine_set=quarantine_set, ledger=ledger
            )

    return state.mutations


def _compute_mutations_emit_local_only(
    key, local_state, jira_state, state, *, quarantine_set, ledger
) -> None:
    """Per-key phase: key present in local only — emit outbound create / conflict / probe."""
    Mutation = state.Mutation
    MutationDirection = state.MutationDirection
    MutationAction = state.MutationAction
    excluded = state.excluded
    duplicate_local_ids = state.duplicate_local_ids
    local_id_to_keys = state.local_id_to_keys
    bound_local_ids = state.bound_local_ids
    mutations = state.mutations

    local_fields = local_state[key] or {}
    local_id_val = local_fields.get("local_id") if isinstance(local_fields, dict) else None
    local_id_str = str(local_id_val) if local_id_val else None

    # dd-5: duplicate local_id collision across local tickets —
    # surface each colliding owner as an (inbound, conflict) Mutation
    # so the human or downstream tooling can disambiguate. Take
    # precedence over the standard outbound-create path because the
    # underlying state is unbindable as-is.
    if local_id_str and local_id_str in duplicate_local_ids:
        colliding = sorted(local_id_to_keys[local_id_str])
        _emit(
            Mutation(
                direction=MutationDirection.inbound,
                action=MutationAction.conflict,
                target=key,
                payload={},
                provenance={
                    "source": "differ",
                    "reason": "duplicate_local_id",
                    "local_id": local_id_str,
                    "colliding_keys": colliding,
                },
            ),
            quarantine_set=quarantine_set,
            mutations_out=mutations,
            ledger=ledger,
        )
        return

    if local_id_val and str(local_id_val) in bound_local_ids:
        # Already mirrored in Jira under a different key — do not
        # emit a redundant outbound create. (dd-4)
        return

    # A Jira key matching this local ID without a reciprocal binding is ambiguous.
    # Probe before creating a possible duplicate.
    if local_id_str and local_id_str in jira_state:
        jira_sibling = jira_state.get(local_id_str) or {}
        sibling_local_id = jira_sibling.get("local_id") if isinstance(jira_sibling, dict) else None
        if not sibling_local_id:
            _emit(
                Mutation(
                    direction=MutationDirection.outbound,
                    action=MutationAction.probe,
                    target=key,
                    payload={},
                    provenance={
                        "source": "differ",
                        "reason": "ambiguous_local_binding",
                        "local_id": local_id_str,
                        "jira_sibling_key": local_id_str,
                    },
                ),
                quarantine_set=quarantine_set,
                mutations_out=mutations,
                ledger=ledger,
            )
            return

    payload = {f: v for f, v in local_fields.items() if f not in excluded}
    if not payload:
        # Only excluded fields → no useful create payload.
        return
    _emit(
        Mutation(
            direction=MutationDirection.outbound,
            action=MutationAction.create,
            target=key,
            payload=payload,
            provenance=_derive_provenance(
                target=key,
                primary_fields=local_fields,
                fallback_fields=None,
                reason="unbound_local",
            ),
        ),
        quarantine_set=quarantine_set,
        mutations_out=mutations,
        ledger=ledger,
    )


def _compute_mutations_emit_jira_only(key, jira_state, state, *, quarantine_set, ledger) -> None:
    """Per-key phase: key present in Jira only — emit inbound create / conflict (dd-5)."""
    Mutation = state.Mutation
    MutationDirection = state.MutationDirection
    MutationAction = state.MutationAction
    excluded = state.excluded
    local_rebar_ids = state.local_rebar_ids
    mutations = state.mutations

    jira_fields = jira_state[key] or {}
    jira_local_id = jira_fields.get("local_id") if isinstance(jira_fields, dict) else None
    jira_local_id_str = str(jira_local_id) if jira_local_id else None

    # Fetcher snapshots omit the local_id property. Canonical or legacy rebar-id
    # labels instead mark issues owned by the binding-aware differs. Suppress their
    # inbound create and dangling conflict. Keep the explicit local_id path for
    # fixture-backed dangling-reference conflicts.
    if jira_local_id is None and isinstance(jira_fields, dict):
        _labels = jira_fields.get("labels") or []
        _has_rebar_id_label = False
        if isinstance(_labels, (list, tuple)):
            for _lbl in _labels:
                if isinstance(_lbl, str) and (
                    _lbl.startswith("rebar-id:") or _lbl.startswith("rebar-id-")
                ):
                    _has_rebar_id_label = True
                    break
        if _has_rebar_id_label:
            # Bound — owned by binding-aware differs.
            return

    # A Jira binding with no matching local ticket needs an inbound conflict for
    # operator resolution.
    if jira_local_id_str and jira_local_id_str not in local_rebar_ids:
        _emit(
            Mutation(
                direction=MutationDirection.inbound,
                action=MutationAction.conflict,
                target=key,
                payload={
                    "jira_field_snapshot": dict(jira_fields),
                },
                provenance={
                    "source": "differ",
                    "reason": "dangling_jira_local_id",
                    "dangling_local_id": jira_local_id_str,
                },
            ),
            quarantine_set=quarantine_set,
            mutations_out=mutations,
            ledger=ledger,
        )
        return

    if jira_local_id_str in local_rebar_ids:
        # The Jira entity property is a positive back-pointer to an existing
        # local ticket. Even if the binding row and identity label are both
        # gone, this remote issue is still managed and must not be adopted as
        # a Jira-native ticket.
        return

    payload = {f: v for f, v in jira_fields.items() if f not in excluded}
    # An inbound create with an empty payload is still meaningful
    # (it announces a new Jira-side issue) — keep the Mutation even
    # if every field is excluded, because the target itself is the
    # signal.
    _emit(
        Mutation(
            direction=MutationDirection.inbound,
            action=MutationAction.create,
            target=key,
            payload=payload,
            provenance=_derive_provenance(
                target=key,
                primary_fields=jira_fields,
                fallback_fields=None,
                reason="jira_new",
            ),
        ),
        quarantine_set=quarantine_set,
        mutations_out=mutations,
        ledger=ledger,
    )


def _compute_mutations_emit_both(
    key, local_state, jira_state, state, *, quarantine_set, ledger
) -> None:
    """Per-key phase: key present in both — field-by-field diff, emit outbound update."""
    Mutation = state.Mutation
    MutationDirection = state.MutationDirection
    MutationAction = state.MutationAction
    excluded = state.excluded
    conflict_resolver = state.conflict_resolver
    mutations = state.mutations

    # Present in both — diff non-excluded fields.
    local_fields = local_state[key] or {}
    jira_fields = jira_state[key] or {}
    changed: dict[str, Any] = {}
    for field in set(local_fields) | set(jira_fields):
        if field in excluded:
            continue
        local_val = local_fields.get(field)
        jira_val = jira_fields.get(field)
        if field == "labels" and _non_internal_labels(local_val) == _non_internal_labels(jira_val):
            # Ignore drift confined to bridge-owned labels. It is prior-pass writeback.
            continue
        if local_val != jira_val:
            if field in conflict_resolver.FIELD_CLASSES:
                changed[field] = conflict_resolver.resolve_field(
                    field, local_val, jira_val, provenance_record=None
                )
            else:
                changed[field] = local_val
    if changed:
        _emit(
            Mutation(
                direction=MutationDirection.outbound,
                action=MutationAction.update,
                target=key,
                payload={
                    "changed_fields": changed,
                    "comments": [],
                    "labels": [],
                    "links": [],
                },
                provenance=_derive_provenance(
                    target=key,
                    primary_fields=jira_fields,
                    fallback_fields=local_fields,
                    reason="field_drift",
                ),
            ),
            quarantine_set=quarantine_set,
            mutations_out=mutations,
            ledger=ledger,
        )


def compute_mutations_with_ledger(
    local_state: dict[str, dict] | None = None,
    jira_state: dict[str, dict] | None = None,
    *,
    ledger: Any | None = None,
    quarantine_set: set[str] | None = None,
    seed_mutations: list[Any] | None = None,
) -> list[Any]:
    """Wrapper around :func:`compute_mutations` that consults an element-level
    `ledger` (from :class:`conflict_resolver.ProvenanceLedger`) to suppress
    echoes of prior local-origin writes on individual collection elements.

    Behavior:
      - Calls compute_mutations with the same args.
      - For each emitted update Mutation whose payload contains
        `changed_fields` for a collection-class field (labels/watchers/links),
        consults `ledger.is_echo(f"{field_name}:{element}", element)` per
        element. Elements found in the ledger are filtered from the changed
        set. If the entire change is echoed, the mutation is suppressed
        entirely.
      - When `ledger` is None, behavior is identical to compute_mutations.
    """
    mutations = compute_mutations(
        local_state=local_state,
        jira_state=jira_state,
        quarantine_set=quarantine_set,
        seed_mutations=seed_mutations,
        ledger=None,  # element-level ledger applied below; do not double-suppress
    )
    if ledger is None or not hasattr(ledger, "serialize"):
        return mutations

    # Index fields with recorded local-origin elements. Their next-pass readback
    # must not emit an outbound update before Jira catches up.
    ledger_keys = set(ledger.serialize().keys()) if hasattr(ledger, "serialize") else set()
    ledger_fields = {k.split(":", 1)[0] for k in ledger_keys if ":" in k}

    surviving: list[Any] = []
    for m in mutations:
        action_value = getattr(m.action, "value", str(m.action))
        if action_value != "update":
            surviving.append(m)
            continue
        payload = dict(m.payload or {})
        changed = payload.get("changed_fields") or payload
        if not isinstance(changed, dict):
            surviving.append(m)
            continue
        # Suppress an update touching a field tracked at element level by the ledger.
        touched_fields = set(changed.keys())
        if touched_fields & ledger_fields:
            continue
        surviving.append(m)
    return surviving
