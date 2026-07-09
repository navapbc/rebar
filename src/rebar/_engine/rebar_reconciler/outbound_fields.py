"""Outbound field-diff cluster for bidirectional Jira sync.

The local→Jira field-mapping + field-comparison seam extracted from
``outbound_differ.py`` (it grew past the module-size soft cap). Owns the
local→Jira field name/value mapping (``_map_local_to_jira_fields``), the
Jira-side field extraction (``_extract_jira_field``), the shape-tolerant
assignee match (``_assignee_matches``), the inbound-directionality guard
(``_local_matches_prev`` over the ``_INBOUND_MIRRORED_FIELDS`` set), the
managed-parent-clear gate (``_parent_clear_is_managed``), and the changed-field
diff (``_diff_fields``).

``compute_outbound_mutations`` (in ``outbound_differ``) imports this module; the
dependency is one-way. Like the other reconciler modules, this module keeps its
OWN lazy ``_load_adf`` loader (the differ may be spec-loaded by file path in
tests, where ``from . import`` does not resolve) so it never imports back from
``outbound_differ`` — avoiding an import cycle.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any


def _rebar_env(name: str, default: str | None = None) -> str | None:
    """Read ``REBAR_<name>`` from the environment."""
    return os.environ.get(f"REBAR_{name}", default)


# ``lazy_load`` centralizes the by-path sibling-loader idiom (rebar_reconciler/
# _loader.py). Import it normally when package context exists, else bootstrap it
# by file path — this module is itself exec'd standalone via
# spec_from_file_location in tests.
try:
    from rebar_reconciler._loader import lazy_load
except ImportError:  # standalone load without package context
    _loader_key = "rebar_reconciler._loader"
    if _loader_key not in sys.modules:
        _loader_spec = importlib.util.spec_from_file_location(
            _loader_key, Path(__file__).parent / "_loader.py"
        )
        assert _loader_spec is not None and _loader_spec.loader is not None
        _loader_mod = importlib.util.module_from_spec(_loader_spec)
        sys.modules[_loader_key] = _loader_mod
        _loader_spec.loader.exec_module(_loader_mod)  # type: ignore[union-attr]
    lazy_load = sys.modules[_loader_key].lazy_load


# Lazy-loader singleton for the sibling adf module. Kept module-local (each
# reconciler module owns its own copy) because the differ may be imported via
# ``importlib.util.spec_from_file_location`` in tests, which does not establish
# package context, so ``from . import adf`` would fail.
_ADF_KEY = "rebar_reconciler.adf"
_AdfModule = None


def _load_adf():
    """Lazy-load the sibling adf module (own copy; mirrors the other differs').

    Loaded by the canonical dotted sys.modules key so the module is executed
    exactly once across all callers, whether the differ was imported as a normal
    package module (production) or by file path (tests).
    """
    global _AdfModule
    if _AdfModule is None:
        _AdfModule = lazy_load(_ADF_KEY, "adf.py")
    return _AdfModule


# ---------------------------------------------------------------------------
# Field mapping constants
# ---------------------------------------------------------------------------

_LOCAL_TO_JIRA_TYPE: dict[str, str] = {
    "bug": "Bug",
    "story": "Story",
    "task": "Task",
    "epic": "Epic",
}

_LOCAL_TO_JIRA_PRIORITY: dict[int, str] = {
    0: "Highest",
    1: "High",
    2: "Medium",
    3: "Low",
    4: "Lowest",
}

_LOCAL_TO_JIRA_STATUS: dict[str, str] = {
    "idea": "IDEA",
    "open": "To Do",
    "in_progress": "In Progress",
    # blocked/cancelled have no direct equivalent in the live DIG workflow
    # ({To Do, In Progress, In Review, Done} only). Map to the nearest live
    # state; lossless information is preserved via rebar-status: annotation
    # labels emitted/removed by status logic (see _status_annotation_labels).
    "blocked": "In Progress",
    "closed": "Done",
    "cancelled": "Done",
    "deleted": "Done",
}


# ---------------------------------------------------------------------------
# Field mapping helpers
# ---------------------------------------------------------------------------


def _map_local_to_jira_fields(
    ticket: dict[str, Any],
    binding_store: Any = None,
    local_ticket_types: dict[str, str] | None = None,
    emit_detach_clear: bool = False,
) -> dict[str, Any]:
    """Map local ticket fields to Jira field names/values.

    Use ``.get(key) or default`` (not ``.get(key, default)``) for string
    fields so an explicit ``None`` value normalises to the empty-string
    default. ``.get(key, default)`` only falls back when the key is
    MISSING — if the key exists with value ``None`` (e.g. unassigned
    tickets where the ticket reducer initialises ``assignee: None``),
    .get returns None, not the default. None then propagates through
    ``_diff_fields`` and becomes the literal string ``"None"`` after
    str() conversion at the ACLI boundary, causing ACLI to reject the
    edit with exit 1.

    Parent resolution (ticket 8b25): when ``binding_store`` is supplied and
    the ticket carries a ``parent_id``, attempt to resolve it to a Jira key
    via ``binding_store.get_jira_key(parent_id)``.  An unbound parent is
    silently omitted from the returned dict (not ``None`` / empty-string) so
    the diff layer can distinguish "no parent set" from "parent set but
    unbound this pass" and skip / retry accordingly.
    """
    result: dict[str, Any] = {
        "summary": ticket.get("title") or "",
        "description": ticket.get("description") or "",
        "issuetype": _LOCAL_TO_JIRA_TYPE.get(ticket.get("ticket_type", "task"), "Task"),
        "priority": _LOCAL_TO_JIRA_PRIORITY.get(ticket.get("priority", 2), "Medium"),
        "status": _LOCAL_TO_JIRA_STATUS.get(ticket.get("status", "open"), "To Do"),
        "assignee": ticket.get("assignee") or "",
    }
    # Parent sync (ticket 8b25): resolve local parent_id → Jira key.
    # Omit the key entirely when unbound so _diff_fields skips it (retry next pass).
    local_parent_id = ticket.get("parent_id") or None
    if local_parent_id and binding_store is not None:
        import logging as _logging

        # Hierarchy pre-check (ticket 8b25): on this next-gen Jira project only
        # an Epic may be a parent. A non-epic parent (e.g. Task→Task) is
        # rejected by Jira with HTTP 400. Suppress the parent diff entirely
        # when the resolved local parent's ticket_type != "epic" so the differ
        # never perpetually re-emits a parent mutation Jira will always reject.
        # This is a sync exclusion mirroring the bug-36af issuetype pattern.
        # When parent-type info is unavailable (map not supplied / parent
        # absent from the map), fall through to the existing behaviour rather
        # than guess — the applier-side 400-skip remains the backstop.
        if local_ticket_types is not None and local_parent_id in local_ticket_types:
            parent_type = (local_ticket_types.get(local_parent_id) or "").lower()
            if parent_type != "epic":
                _logging.getLogger(__name__).debug(
                    "_map_local_to_jira_fields: parent_id=%r for ticket %r is a "
                    "non-epic (%r); suppressing parent diff (Jira hierarchy only "
                    "permits Epic parents — sync exclusion, ticket 8b25)",
                    local_parent_id,
                    ticket.get("ticket_id"),
                    parent_type,
                )
                return result

        jira_parent_key = binding_store.get_jira_key(local_parent_id)
        if jira_parent_key:
            result["parent"] = jira_parent_key
        else:
            _logging.getLogger(__name__).debug(
                "_map_local_to_jira_fields: parent_id=%r for ticket %r is unbound "
                "this pass; skipping parent field (will retry next pass)",
                local_parent_id,
                ticket.get("ticket_id"),
            )
    elif binding_store is not None and emit_detach_clear:
        # Symmetric parent CLEAR (parent-detach churn fix): the ticket has been
        # DETACHED locally (parent_id is falsy). Emit an explicit ``None``
        # sentinel into the mapped dict so the field-diff loop's ``parent``
        # branch runs and compares None against the Jira-side parent key:
        # it emits a CLEAR only when Jira still carries a parent (stale epic-
        # link) and nothing when both sides are already parent-less (so a
        # never-parented ticket does not churn a clear every pass).
        # Gated on binding_store (the parent-sync feature seam) AND on
        # ``emit_detach_clear`` — only the UPDATE diff path (``_diff_fields``)
        # opts in; the CREATE path leaves the key ABSENT so an orphan create
        # never carries a spurious ``parent: None`` payload. The hierarchy
        # pre-check above is intentionally skipped for a clear — there is no
        # parent type to validate.
        result["parent"] = None
    return result


def _extract_jira_field(jira_fields: dict[str, Any], field: str) -> Any:
    """Extract a Jira field value, handling nested structures.

    Jira API returns some fields as nested objects (priority.name,
    issuetype.name, status.name, assignee.displayName), and description is
    returned as an Atlassian Document Format (ADF) dict, not a plain string.

    Bug 85a1: before this fix, description ADF dicts were extracted as
    ``""`` because the generic ``raw.get("name", raw.get("displayName", ""))``
    fallback found neither key on ADF (``{"type": "doc", "version": 1,
    "content": [...]}``). The differ then reported description as changed on
    every pass for every bound ticket — the 21-mutation idempotency churn
    documented in the e2e probe Phase 6.

    Fix: dispatch by field name. Description ADF dicts are decoded via
    ``adf.adf_to_text``; assignee continues to return ``displayName`` (the
    canonical form local probe tickets store); priority / status / issuetype
    use the existing ``.name`` extraction. Plain string values (including
    legacy snapshots from before ADF migration) pass through unchanged.
    """
    raw = jira_fields.get(field)
    if raw is None:
        return ""

    # Description: ADF dict → plain text via the project's ADF walker.
    if field == "description":
        if isinstance(raw, dict):
            return _load_adf().adf_to_text(raw)
        return raw  # legacy plain-string snapshot

    if isinstance(raw, dict):
        # Jira nested objects: {name: ..., id: ...}
        return raw.get("name", raw.get("displayName", ""))
    return raw


def _assignee_matches(local_val: str, jira_raw: Any) -> bool:
    """Permissive assignee equality (bug 85a1, Gap 4).

    Jira returns assignee as a dict with at least ``{accountId, displayName,
    emailAddress}``; local tickets store assignee as a bare string that may
    be an email (ticket-create.sh default), a displayName (probe), or
    "Test" (git-config default), depending on how the ticket was made.
    A direct ``local_val == _extract_jira_field(...)`` comparison fires on
    every pass for any user not stored under the same identity form as
    Jira returns — Phase 6 idempotency churn AND spurious outbound updates.

    Treat ``local_val`` as matching when it equals ANY of {emailAddress,
    accountId, displayName}. Both sides empty (unassigned) also match.
    """
    if jira_raw is None:
        return local_val == ""
    if not isinstance(jira_raw, dict):
        return local_val == str(jira_raw)
    candidates = {
        (jira_raw.get("emailAddress") or "").strip(),
        (jira_raw.get("accountId") or "").strip(),
        (jira_raw.get("displayName") or "").strip(),
    }
    candidates.discard("")
    return (local_val or "").strip() in candidates


# Fields the INBOUND differ mirrors Jira→local (inbound_differ.py field_map). A
# Jira-side change to one of these, when local is unchanged since the last sync,
# should flow inbound rather than be reverted by local-wins (inbound field-sync
# fix). Parent/issuetype/links are NOT inbound-mirrored and keep local-wins.
_INBOUND_MIRRORED_FIELDS = frozenset({"title", "description", "priority", "status", "assignee"})


def _local_matches_prev(field_name: str, local_val: Any, prev_jira_fields: dict[str, Any]) -> bool:
    """True when the local value equals the LAST-SYNCED Jira value (prev_snapshot).

    If local equals what Jira had at the previous pass, local has NOT changed since
    the last sync — so any difference from *current* Jira is a Jira-side edit, which
    must be mirrored inbound, not reverted. Uses the same shape-tolerant comparisons
    as the current-Jira diff (assignee dict-vs-string; rstrip for text). Returns
    False when there is no prev entry (degrade to local-wins — no regression).
    """
    if not prev_jira_fields:
        return False
    if field_name == "assignee":
        return _assignee_matches(local_val, prev_jira_fields.get("assignee"))
    prev_val = _extract_jira_field(prev_jira_fields, field_name)
    if isinstance(local_val, str) and isinstance(prev_val, str):
        return local_val.rstrip() == prev_val.rstrip()
    return local_val == prev_val


def _jira_matches_prev(
    field_name: str, jira_fields: dict[str, Any], prev_jira_fields: dict[str, Any]
) -> bool:
    """True when CURRENT Jira equals the LAST-SYNCED Jira value (baseline).

    The Jira-side mirror of :func:`_local_matches_prev`: ``False`` means Jira has been
    edited since the last sync. Used only to detect a both-sides conflict (bug a713);
    returns ``True`` (no detectable Jira change) when there is no baseline, so an
    absent baseline never fabricates a conflict."""
    if not prev_jira_fields:
        return True
    if field_name == "assignee":
        jira_assignee: Any = jira_fields.get("assignee")
        return _assignee_matches(jira_assignee, prev_jira_fields.get("assignee"))
    cur = _extract_jira_field(jira_fields, field_name)
    prev = _extract_jira_field(prev_jira_fields, field_name)
    if isinstance(cur, str) and isinstance(prev, str):
        return cur.rstrip() == prev.rstrip()
    return cur == prev


def _parent_clear_is_managed(
    jira_parent_key: str, ticket: dict[str, Any], binding_store: Any
) -> bool:
    """Whether a detached-locally Jira parent is one we MANAGED (so its CLEAR may propagate).

    The parent half of the shared managed-ref removal gate (story safe-luge-nog). Maps the
    Jira parent key back to a local id and asks the provider-agnostic gate whether that
    ``parent`` ref is in the ticket's ``managed_refs``. Fail-open toward NOT clobbering: if
    the local id can't be resolved (no ``get_local_id`` / unbound), treat as unmanaged so a
    human-set Jira parent is left for inbound ADOPT rather than cleared. Local import keeps
    the differ free of module-scope heavy imports (it is loaded standalone in tests)."""
    from rebar.reducer._managed_refs import should_propagate_removal

    get_local_id = getattr(binding_store, "get_local_id", None)
    if get_local_id is None:
        return False
    parent_local_id = get_local_id(jira_parent_key)
    if not parent_local_id:
        return False
    return should_propagate_removal("parent", parent_local_id, ticket)


def _diff_fields(
    ticket: dict[str, Any],
    jira_fields: dict[str, Any],
    binding_store: Any = None,
    local_ticket_types: dict[str, str] | None = None,
    assignee_resolver: Any = None,
    jira_key: str = "",
    prev_jira_fields: dict[str, Any] | None = None,
    conflict_sink: list[tuple[str, str]] | None = None,
    dropped_field_sink: list[tuple[str, str]] | None = None,
    local_id: str = "",
    baseline_consumer_swap: bool = False,
) -> dict[str, Any]:
    """Compare local ticket to Jira fields. Return only changed fields.

    Observability sinks (append-only; behavior unchanged when omitted):
    ``dropped_field_sink`` collects ``(jira_key, field)`` for a mapped field that the
    outbound allowlist excludes yet differs from Jira (bug acd0 — a silent drop);
    ``conflict_sink`` collects ``(jira_key, field)`` for a both-sides conflict —
    ``local != baseline AND jira != baseline`` — where local-wins silently overwrites
    a concurrent Jira edit (bug a713). Local-wins is preserved either way.

    Uses local-wins: if local differs, push outbound regardless of Jira state.
    Assignee comparison is shape-tolerant via ``_assignee_matches`` so
    Jira's ``{accountId, displayName, emailAddress}`` dict matches a local
    string in any of those three forms (bug 85a1, Gap 4).

    Bug b859 (Part 0d): when ``REBAR_RECONCILER_VERBOSE=1`` is set, emit a
    one-line RECON record per detected field-diff with truncated local /
    jira values so operators can debug parity issues directly from the
    probe's side-car log. Off by default to keep production stderr quiet.

    Parent diff (ticket 8b25): when ``binding_store`` is provided and the
    ticket carries a ``parent_id``, the resolved Jira parent key is diffed
    against ``jira_fields["parent"]["key"]``.  Unbound parents are omitted
    from the mapped dict and therefore never emitted as changes.
    """
    verbose = _rebar_env("RECONCILER_VERBOSE", "0") == "1"
    ticket_id = ticket.get("ticket_id") or ticket.get("id") or "<no-id>"

    # Convergence rollout Phase-3 (story a118): the arbitration ANCESTOR used by
    # direction-suppression (Site A) and both-sides-conflict detection (Site B).
    # Flag OFF (default): the prev_snapshot-derived ``prev_jira_fields`` — the swap
    # is byte-for-byte a no-op. Flag ON: the per-binding baseline (get_baseline),
    # which is JIRA-keyed with the same shape (_BASELINE_FIELDS). A ``None`` baseline
    # (no ancestor recorded) is the documented local-wins signal (ADR 0026 §2); a
    # corrupt bindings.json has already failed the pass CLOSED at load, so no new
    # corrupt-detection branch is needed here.
    arbitration_prev = prev_jira_fields
    if baseline_consumer_swap and binding_store is not None and local_id:
        arbitration_prev = binding_store.get_baseline(local_id)

    local_mapped = _map_local_to_jira_fields(
        ticket,
        binding_store=binding_store,
        local_ticket_types=local_ticket_types,
        emit_detach_clear=True,
    )
    changed: dict[str, Any] = {}
    for field_name, local_val in local_mapped.items():
        # Bug 36af: ticket_type/issuetype is governed by an approved sync
        # exception — updates do NOT propagate in either direction once
        # the ticket is bound. The local 'epic' type has no faithful Jira
        # reverse-mapping and Jira workflows often reject issuetype edits
        # cross-hierarchy (Bug<->Epic). issuetype IS still emitted at
        # CREATE time (it's a Jira-required field for issue creation),
        # but the diff loop here only runs for bound update mutations,
        # so excluding the field here only affects updates.
        if field_name == "issuetype":
            # acd0: this is the one place the outbound path drops a *mapped* field
            # (the allowlist exclusion). Surface it when it actually differs from Jira
            # so the silent drop is observable (deduped downstream); do NOT emit it.
            if dropped_field_sink is not None and jira_key:
                jira_it = _extract_jira_field(jira_fields, "issuetype")
                if isinstance(jira_it, dict):
                    jira_it = jira_it.get("name")
                if local_val and jira_it and str(local_val).lower() != str(jira_it).lower():
                    dropped_field_sink.append((jira_key, "issuetype"))
            continue
        # Inbound-sync directionality (Jira-side edits were reverted). For a field
        # the inbound differ can mirror, if local is UNCHANGED since the last sync
        # (matches prev_snapshot) then any difference from current Jira is a
        # Jira-side edit — suppress the outbound here so the inbound differ mirrors
        # it to local instead of local-wins clobbering it. When local has changed
        # (local != prev), fall through to the normal local-wins emit. Degrades to
        # local-wins when there is no prev entry, so it never regresses.
        if field_name in _INBOUND_MIRRORED_FIELDS and _local_matches_prev(
            field_name, local_val, arbitration_prev or {}
        ):
            continue
        if field_name == "assignee":
            jira_assignee = jira_fields.get("assignee")
            if _assignee_matches(local_val, jira_assignee):
                continue  # equivalent identity (or both unassigned) — no churn
            # The differ would otherwise emit an assignee update. Bug 9b94: if the
            # local assignee maps to NO assignable Jira user (e.g. an agent identity
            # like "claude"), the desired state is "unassigned" — so converge to
            # unassigned instead of re-emitting an unsatisfiable assign every pass.
            # Resolution runs ONLY on this would-emit path (rare after pass 1) and
            # only when a resolver is supplied (the live pass); the no-resolver
            # fixture path keeps the legacy permissive string-match behavior.
            if local_val and assignee_resolver is not None:
                acct, authoritative = assignee_resolver(local_val, jira_key)
                if authoritative:
                    current_acct = (
                        jira_assignee.get("accountId") if isinstance(jira_assignee, dict) else None
                    )
                    if (acct or None) == (current_acct or None):
                        continue  # resolved identity already correct — converged
                    if acct is None:
                        # Unmappable local assignee → desired unassigned. Jira has
                        # someone (else we'd have converged above) — clear it.
                        changed[field_name] = ""
                        if verbose:
                            print(  # noqa: T201
                                f"RECON: field_diff ticket={ticket_id} field=assignee "
                                f"local={local_val!r:.80} -> unassign (unmappable)",
                                file=sys.stderr,
                            )
                        continue
                    # Resolvable but mismatched → assign (applier re-resolves the string).
            changed[field_name] = local_val
            if verbose:
                print(  # noqa: T201
                    f"RECON: field_diff ticket={ticket_id} "
                    f"field=assignee local={local_val!r:.80} "
                    f"jira={jira_assignee!r:.80}",
                    file=sys.stderr,
                )
            continue
        if field_name == "parent":
            # Jira returns parent as {"key": "DIG-N"}; local_val is the resolved
            # Jira key string. Extract the Jira-side parent key for comparison.
            jira_parent_raw = jira_fields.get("parent")
            jira_parent_key = (
                jira_parent_raw.get("key") if isinstance(jira_parent_raw, dict) else None
            )
            if local_val != jira_parent_key:
                # Managed-ref gate (tan-elbow-mica): a parent CLEAR (local detached,
                # ``local_val`` falsy) must only propagate when we MANAGED that parent —
                # otherwise a parent a human set directly in Jira (one local never had)
                # would be clobbered instead of ADOPTED inbound. A parent SET (re-parent,
                # ``local_val`` truthy) is always local-authoritative and not gated.
                if (
                    not local_val
                    and jira_parent_key
                    and not _parent_clear_is_managed(jira_parent_key, ticket, binding_store)
                ):
                    continue  # never managed this Jira parent -> adopt inbound, don't clear
                changed[field_name] = local_val
                if verbose:
                    print(  # noqa: T201
                        f"RECON: field_diff ticket={ticket_id} "
                        f"field=parent local={local_val!r:.80} "
                        f"jira={jira_parent_key!r:.80}",
                        file=sys.stderr,
                    )
            continue
        jira_val = _extract_jira_field(jira_fields, field_name)
        # Convergence (bug 626d follow-up): the send path truncates an over-length
        # description so its ADF representation fits Jira's limit, so the refetched
        # Jira value is the truncated form. Apply the IDENTICAL shared ADF-aware fit
        # to the local value before comparing; otherwise an oversized local
        # description never matches the landed Jira body and the differ re-emits an
        # update every pass.
        if field_name == "description" and isinstance(local_val, str):
            local_val = _load_adf().fit_text_to_adf_limit(local_val)
        # Bug (plateau): Jira's ADF normalization strips trailing
        # whitespace from descriptions (and titles) on every write. If
        # local carries trailing ``\n\n`` (or any trailing whitespace),
        # the next fetch returns the stripped form — diff fires again —
        # apply pushes the original — infinite phantom-mutation loop.
        # Discovered during 20-batch live verification (2026-05-29):
        # DIG-4175 plateaued at 339 outbound updates for batches 7-20
        # because local description was 3701 chars, jira-decoded was
        # 3699 (delta = trailing ``\n\n``). Compare with rstrip() so
        # trailing-whitespace differences don't trigger the diff.
        if (
            isinstance(local_val, str)
            and isinstance(jira_val, str)
            and local_val.rstrip() == jira_val.rstrip()
        ):
            continue
        if local_val != jira_val:
            changed[field_name] = local_val
            if verbose:
                # Truncate value repr to keep one-line records reasonable.
                _l = repr(local_val)
                _j = repr(jira_val)
                if len(_l) > 80:
                    _l = _l[:77] + "..."
                if len(_j) > 80:
                    _j = _j[:77] + "..."
                print(  # noqa: T201
                    f"RECON: field_diff ticket={ticket_id} field={field_name} local={_l} jira={_j}",
                    file=sys.stderr,
                )
    # a713: a both-sides conflict — local AND Jira both diverged from the last-synced
    # baseline — means local-wins is silently overwriting a concurrent Jira edit.
    # Record it for an observable conflict signal; local-wins itself is unchanged.
    # Story a118: the ENTIRE Site-B use of the ancestor swaps to arbitration_prev
    # under the flag — the outer truthiness guard AND both matcher args. Flag OFF:
    # arbitration_prev IS prev_jira_fields (identical). A None/{} baseline makes the
    # outer guard falsy (skip conflict detection) — correct local-wins (no ancestor
    # → no concurrent-edit conflict to record).
    if conflict_sink is not None and arbitration_prev and jira_key:
        for fname in changed:
            if (
                fname in _INBOUND_MIRRORED_FIELDS
                and not _local_matches_prev(fname, local_mapped.get(fname), arbitration_prev)
                and not _jira_matches_prev(fname, jira_fields, arbitration_prev)
            ):
                conflict_sink.append((jira_key, fname))
    return changed
