"""Pure-function differ for rebar_reconciler.

Compares the local source-of-truth state against the Jira working-set state and
emits a deterministic list of Mutation objects describing the reconciliation
work to perform.

This module replaces the legacy snapshot-diff contract
(``compute_mutations(prev_snapshot, next_snapshot) -> list[dict]``) with the
new Mutation-based contract:

    compute_mutations(local_state, jira_state) -> list[Mutation]

Fields listed in ``EXCLUDED_FIELDS`` (from ``config.py``) are ignored during
field-level comparison and never appear in a Mutation's payload.

The function is pure: no I/O, no time/random, no logging, no globals.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_sibling(module_name: str, file_name: str) -> ModuleType:
    """Load a sibling module under a stable cache key without PYTHONPATH."""
    sibling_path = Path(__file__).parent / file_name
    cache_key = f"rebar_reconciler_{module_name}"
    spec = importlib.util.spec_from_file_location(cache_key, sibling_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(cache_key, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _load_config() -> ModuleType:
    return _load_sibling("config", "config.py")


def _load_conflict_resolver() -> ModuleType:
    return _load_sibling("conflict_resolver", "conflict_resolver.py")


_MUTATION_KEY = "rebar_reconciler.mutation"


def _load_mutation() -> ModuleType:
    # Prefer an already-loaded mutation module to preserve class identity
    # across callers. Test fixtures load mutation.py under the bare ``mutation``
    # key (and historically under other private keys); production code loads
    # under the canonical ``rebar_reconciler.mutation`` key.
    # Check the test-friendly keys FIRST so a test rig that pre-seeded its own
    # module wins identity ties — otherwise tests that compare against their
    # own freshly loaded ``Mutation`` class fail with cross-identity errors.
    #
    # When no cache hit exists, load under the canonical key so future cross-
    # module lookups (invariants.py, applier.py) share the SAME module object.
    for cache_key in (
        "mutation",
        "rebar_reconciler.mutation",
        "rebar_reconciler_mutation",
        _MUTATION_KEY,
    ):
        cached = sys.modules.get(cache_key)
        if cached is not None and hasattr(cached, "Mutation"):
            return cached
    sibling_path = Path(__file__).parent / "mutation.py"
    spec = importlib.util.spec_from_file_location(_MUTATION_KEY, sibling_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MUTATION_KEY] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# Bridge-internal label prefixes — written by the reconciler itself
# (rebar-id:/rebar-id- identity labels from _apply_inbound_create /
# create_one; rebar-status: annotation labels from outbound status logic;
# imported: local-only bootstrap tags). Mirrors the _EXCLUDED_PREFIXES
# tuples in outbound_differ.py / inbound_differ.py. The snapshot differ
# compares the PREVIOUS pass's Jira snapshot against the current one, so
# our own label write-back from the prior pass surfaces here as apparent
# remote drift one pass later — and emitted a spurious (outbound, update)
# echo for every freshly bound/imported issue (ticket robe-creek-zealot).
_BRIDGE_INTERNAL_LABEL_PREFIXES: tuple[str, ...] = (
    "rebar-id:",
    "rebar-id-",
    "imported:",
    "rebar-status:",
)


def _non_internal_labels(value: Any) -> set[str]:
    """The label set minus bridge-internal entries (non-list values → empty)."""
    if not isinstance(value, (list, tuple)):
        return set()
    return {
        v
        for v in value
        if isinstance(v, str) and not v.startswith(_BRIDGE_INTERNAL_LABEL_PREFIXES)
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


def compute_mutations(
    local_state: dict[str, dict] | None = None,
    jira_state: dict[str, dict] | None = None,
    *,
    quarantine_set: set[str] | None = None,
    seed_mutations: list[Any] | None = None,
    ledger: Any | None = None,
) -> list[Any]:
    """Diff local against jira state and return a list of Mutation objects.

    Args:
        local_state: ``{key: {field: value, ...}}`` — the local source of
            truth (e.g. the ticket tracker snapshot).
        jira_state: ``{key: {field: value, ...}}`` — the Jira working set
            recently fetched.
        quarantine_set: Optional set of targets whose mutations must be
            suppressed. Every emit point routes through :func:`_emit`,
            which drops any mutation whose ``target`` is in this set. When
            ``None`` (the default), no suppression is performed.
        seed_mutations: Optional list of pre-built Mutations to prepend to
            the result. Used by ``invariants.check_dual_identity_complete``
            (story 7a75) to inject repair/inbound mutations that the differ
            itself cannot derive from local/jira state alone. Seed
            mutations are NOT filtered through ``quarantine_set``.

    Returns:
        A list of ``Mutation`` objects. Seed mutations (if any) appear
        first, followed by differ-emitted mutations sorted by ``target``
        for determinism. Each Mutation carries a non-empty provenance
        Mapping.

    Semantics:
        - Key in ``local_state`` only AND its ``local_id`` is not
          already bound in ``jira_state`` → outbound create.
        - Key in ``local_state`` only but its ``local_id`` IS already
          bound in ``jira_state`` → skipped (already mirrored).
        - Key in ``jira_state`` only → inbound create.
        - Key in both → field-by-field diff (excluding ``EXCLUDED_FIELDS``);
          if any non-excluded field differs, emit an outbound update whose
          payload contains only the resolved changed fields.
        - A create whose only fields are in ``EXCLUDED_FIELDS`` is
          suppressed (no information would survive the field filter).

    This function is pure: no I/O, no time, no logging, no globals.
    """
    if local_state is None:
        local_state = {}
    if jira_state is None:
        jira_state = {}

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
    duplicate_local_ids: set[str] = {
        lid for lid, keys in local_id_to_keys.items() if len(keys) > 1
    }
    # The set of local local_ids that are present in local_state at all,
    # used to short-circuit dangling-jira-ref detection.
    local_rebar_ids: set[str] = set(local_id_to_keys.keys())

    # Seed mutations (if any) are prepended to the result list before the
    # differ walks local/jira state. They are NOT filtered through
    # quarantine_set — the caller (e.g. invariants.check_dual_identity_complete)
    # has authority over what it injects.
    mutations: list[Any] = list(seed_mutations) if seed_mutations else []
    all_keys = set(local_state) | set(jira_state)

    for key in sorted(all_keys):
        in_local = key in local_state
        in_jira = key in jira_state

        if in_local and not in_jira:
            local_fields = local_state[key] or {}
            local_id_val = (
                local_fields.get("local_id")
                if isinstance(local_fields, dict)
                else None
            )
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
                continue

            if local_id_val and str(local_id_val) in bound_local_ids:
                # Already mirrored in Jira under a different key — do not
                # emit a redundant outbound create. (dd-4)
                continue

            # dd-5: ambiguous local binding — local ticket carries a
            # local_id that matches the KEY of an unrelated Jira issue
            # (an issue that exists in jira_state but does NOT carry a
            # back-pointer local_id binding). This suggests a possibly
            # stale or conflated binding: the local_id may once have referred
            # to that Jira issue, but the Jira side no longer agrees. Emit
            # (outbound, probe) so the applier can disambiguate before
            # blindly creating a duplicate Jira issue.
            #
            # Design choice: a bare unbound_local with local_id and no
            # jira-side signal is treated as a normal outbound create
            # (preserving existing test_differ.py semantics) — the ambiguity
            # signal is the presence of a jira_state entry under the same
            # key as the local_id, without a reciprocal binding.
            if local_id_str and local_id_str in jira_state:
                jira_sibling = jira_state.get(local_id_str) or {}
                sibling_local_id = (
                    jira_sibling.get("local_id")
                    if isinstance(jira_sibling, dict)
                    else None
                )
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
                    continue

            payload = {
                f: v
                for f, v in local_fields.items()
                if f not in excluded
            }
            if not payload:
                # Only excluded fields → no useful create payload.
                continue
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
        elif in_jira and not in_local:
            jira_fields = jira_state[key] or {}
            jira_local_id = (
                jira_fields.get("local_id")
                if isinstance(jira_fields, dict)
                else None
            )
            jira_local_id_str = str(jira_local_id) if jira_local_id else None

            # Bug 4354: when the snapshot lacks local_id (the fetcher
            # stores Jira `fields` only — the local_id entity property
            # is never in the snapshot), fall back to the `rebar-id:<local_id>`
            # / `rebar-id-<local_id>` label as the bound-marker signal. The
            # same prefixes are excluded by outbound_differ._EXCLUDED_PREFIXES
            # and inbound_differ._EXCLUDED_PREFIXES — they're the canonical
            # and legacy forms written by _apply_inbound_create / create_one
            # (see applier.py:676 and applier.py:1825).
            #
            # When a label-derived binding is found, the issue is OWNED by
            # the binding-aware differs (outbound_differ + inbound_differ
            # in reconcile.py:617/703) — the snapshot-differ MUST stand
            # down. Without this suppression, every bound issue that
            # appears in curr_snapshot but not prev_snapshot (e.g. the pass
            # immediately after outbound binding, before prev advances)
            # mis-classifies as unbound and emits an inbound CREATE — which
            # the applier materialises as a phantom `jira-dig-NNNN` local
            # entity AND writes a ghost `rebar-id:jira-dig-NNNN` label back
            # to Jira (empirically confirmed by labels-probe.sh on
            # 2026-05-29: after binding 259f-... → DIG-5029, the next pass
            # produced `['rebar-id:259f-...', 'rebar-id:jira-dig-5029',
            # 'labelprobe-...']` on Jira). The label-derived path also
            # suppresses the dangling-conflict emission below: a
            # conflict's `suppress_pair` follow-on would otherwise drop
            # the legitimate inbound_differ update for the same bound
            # ticket (the T3 inbound-add label propagation failure).
            #
            # The label-derived suppression is intentionally scoped to the
            # label fallback path. When local_id IS present on the
            # snapshot entry (only via test fixtures, since the fetcher
            # never writes it), preserve the existing dd-5 dangling-jira-ref
            # semantics — a Jira issue that claims a binding via the
            # explicit property but has no matching local ticket should
            # still surface as inbound conflict so it isn't silently
            # dropped.
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
                    continue

            # dd-5: dangling jira ref — the Jira issue claims a binding to a
            # local_id that has no matching local ticket. Surface as
            # (inbound, conflict) so the human can decide whether to recreate
            # the local ticket, clear the Jira-side binding, or close the
            # Jira issue. Never silently drop.
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
                continue

            payload = {
                f: v
                for f, v in jira_fields.items()
                if f not in excluded
            }
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
        else:
            # Present in both — diff non-excluded fields.
            local_fields = local_state[key] or {}
            jira_fields = jira_state[key] or {}
            changed: dict[str, Any] = {}
            for field in set(local_fields) | set(jira_fields):
                if field in excluded:
                    continue
                local_val = local_fields.get(field)
                jira_val = jira_fields.get(field)
                if field == "labels" and _non_internal_labels(
                    local_val
                ) == _non_internal_labels(jira_val):
                    # Label drift confined to bridge-internal labels is our
                    # own write-back from the prior pass (rebar-id: identity,
                    # rebar-status: annotations) — not remote drift. Emitting
                    # it produced a spurious one-pass-later (outbound, update)
                    # echo per freshly bound issue (ticket robe-creek-zealot).
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
                        payload=changed,
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

    # Symmetric inbound-probe pass: a local ticket may carry a bound
    # ``jira_key`` pointing at a Jira issue that is ABSENT from the current
    # jira_state working set. This is the inbound counterpart to the
    # ambiguous_local_binding (outbound, probe) emission above — the local
    # side believes a partner exists, but the Jira working set does not
    # surface it. Emit (inbound, probe) so the applier can investigate
    # (deleted? out of scope of working-set query? renamed?).
    for local_key in sorted(local_state):
        local_entry = local_state[local_key]
        if not isinstance(local_entry, dict):
            continue
        bound_jira_key = local_entry.get("jira_key")
        if not bound_jira_key:
            continue
        bound_jira_key_str = str(bound_jira_key)
        if bound_jira_key_str in jira_state:
            continue
        _emit(
            Mutation(
                direction=MutationDirection.inbound,
                action=MutationAction.probe,
                target=bound_jira_key_str,
                payload={"reason": "absent_partner"},
                provenance={
                    "source": "differ",
                    "local_target": local_key,
                },
            ),
            quarantine_set=quarantine_set,
            mutations_out=mutations,
            ledger=ledger,
        )

    return mutations


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

    # Build a quick lookup of `field_name -> set of recorded element_keys` so we
    # can detect "this mutation touches a field that has recent local-origin
    # ledger entries on its elements". When the mutation is an outbound update
    # on a collection field that the ledger already tracked, the mutation is
    # an echo and we suppress it. Per the story-26de echo-suppression DD: a
    # write recorded as local-origin on pass N must not re-emit on pass N+1
    # even if the jira snapshot has not yet caught up.
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
        # If the mutation touches any field that the ledger tracks at the
        # element level, treat it as an echo of our prior local-origin write
        # and suppress the entire mutation. This is the per-element echo
        # contract from story 26de.
        touched_fields = set(changed.keys())
        if touched_fields & ledger_fields:
            continue
        surviving.append(m)
    return surviving
