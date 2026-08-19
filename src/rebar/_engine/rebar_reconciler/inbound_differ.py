"""Inbound differ for bidirectional Jira sync.

Detects Jira-side changes for bound tickets and emits inbound update mutations
to apply to the local ticket system. Only processes tickets that are already
bound in the BindingStore — unbound Jira issues are ignored (local is source
of truth; they will be handled by outbound creates).

Conflict resolution: local wins. When both local and Jira have changed a
field, the change is skipped (the outbound differ will push the local value).

This module is pure: no I/O, no time/random, no logging, no globals.

Dependency: BindingStore interface (PR #401). This module codes against the
interface — get_local_id(jira_key) -> str|None — and does not import the
concrete class.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

# The Jira->local field/status translation layer lives in the sibling
# ``inbound_fields`` leaf module (module-size split, epic 716f). Imported back
# and re-exported here so ``inbound_differ.<symbol>`` attribute access (and the
# config parity tests) keep resolving unchanged. The Jira->local field MAPPER
# (_map_jira_to_local_fields) is NO LONGER imported here (ticket 4af8): the vendor
# mapper is injected as ``inbound_mapper`` (a Backend-port ``InboundMapper``) by the
# orchestrator that owns the configured backend, so this core differ names no vendor
# mapper. The remaining imports are non-mapping value maps / helpers with no Backend
# -port equivalent, kept for attribute access + the config parity tests.
from rebar_reconciler.inbound_fields import (  # noqa: F401
    _ADF_KEY_INBOUND,
    _JIRA_TO_LOCAL_PRIORITY,
    _JIRA_TO_LOCAL_STATUS,
    _JIRA_TO_LOCAL_TYPE,
    _REBAR_STATUS_LABEL_TO_LOCAL,
    _assignee_matches,
    _description_forms,
    _extract_jira_field_value,
    _load_adf,
    _normalize_jira_body,
    classify_inbound_parent,
    diff_inbound_parent,
)

if TYPE_CHECKING:
    from rebar_reconciler._backend import InboundMapper, OutboundMapper

# Reconciler loop-breaker marker (Gap 1). Outbound comments embed this
# token; inbound passes filter any Jira comment whose body contains it
# so we do not detect our own echoes as new Jira-side comments.
RECONCILER_MARKER = "<!-- rebar:reconciler-echo -->"


_LINK_DIR_KEY = "rebar_reconciler.link_direction"
_LinkDirModule = None


def _load_link_direction():
    """Lazy-load the sibling link_direction module (mirrors _load_adf)."""
    global _LinkDirModule
    if _LinkDirModule is not None:
        return _LinkDirModule
    if _LINK_DIR_KEY in sys.modules:
        _LinkDirModule = sys.modules[_LINK_DIR_KEY]
        return _LinkDirModule
    path = Path(__file__).parent / "link_direction.py"
    spec = importlib.util.spec_from_file_location(_LINK_DIR_KEY, path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"link_direction.py not found at {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_LINK_DIR_KEY] = mod
    spec.loader.exec_module(mod)
    _LinkDirModule = mod
    return mod


# ---------------------------------------------------------------------------
# BindingStore protocol — codes against PR #401's interface
# ---------------------------------------------------------------------------


@runtime_checkable
class BindingStoreProtocol(Protocol):
    """Minimal interface for the inbound binding store lookup."""

    def get_local_id(self, jira_key: str) -> str | None: ...


# ---------------------------------------------------------------------------
# InboundMutation dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InboundMutation:
    """A single inbound change to apply to the local ticket system."""

    jira_key: str
    local_id: str
    action: str  # "update"
    fields: dict[str, Any]  # changed fields only
    comments: list[dict[str, Any]] = dataclass_field(default_factory=list)
    labels: list[dict[str, Any]] = dataclass_field(default_factory=list)
    links: list[dict[str, Any]] = dataclass_field(default_factory=list)


def _diff_jira_vs_local(
    jira_fields: dict[str, Any],
    local_ticket: dict[str, Any],
    binding_store: Any = None,
    *,
    inbound_mapper: InboundMapper | None = None,
    outbound_mapper: OutboundMapper | None = None,
) -> dict[str, Any]:
    """Compare Jira fields to local ticket. Return fields where Jira differs.

    Only returns fields where the Jira value (mapped to local conventions)
    differs from the current local value.

    Parent sync (ticket 8b25): when ``binding_store`` is provided, the Jira
    ``parent`` field is resolved to a local id and compared against
    ``local_ticket["parent_id"]``.  Unbound parent keys are omitted (not
    emitted as changes) so the next pass can retry once the parent is bound.
    """
    # Ticket 4af8: the remote->local mapper is injected. compute_inbound_mutations
    # passes it down; a direct caller (unit test) that omits it falls back to the
    # configured backend's mapper via the neutral registry seam (no vendor mapper is
    # named in this core differ).
    if inbound_mapper is None:
        from rebar.config import compose_config
        from rebar_reconciler._backend_registry import select_backend

        inbound_mapper = select_backend(compose_config()).inbound
    jira_mapped = inbound_mapper.map_remote_to_local(jira_fields)
    changed: dict[str, Any] = {}

    # Bug 36af: ticket_type is governed by an approved sync exception —
    # outbound updates do NOT propagate local->Jira because Jira's coarser
    # type taxonomy (Bug/Story/Task/Epic) is not a faithful reverse-mapping
    # for the richer local types (e.g. 'epic' as a planning container).
    # The inbound mirror was missed: without exclusion here, a Jira-side
    # 'Bug' overwrites a local 'epic' on the next pass, corrupting state.
    # See bug 36af-cb85-374e-4d2e for the live fleet evidence (DIG-4346 and
    # DIG-4473 both queued epic->bug mutations).
    field_map = {
        "title": "title",
        "description": "description",
        "priority": "priority",
        "status": "status",
        "assignee": "assignee",
    }

    for local_field, ticket_field in field_map.items():
        # Assignee: shape-tolerant equality. A live Jira fetch returns the
        # assignee as a {accountId, displayName, emailAddress} dict while local
        # stores a bare string in any one of those forms. Compare against the
        # RAW Jira value (not the displayName-only mapped value) so a local
        # email matching the Jira dict's emailAddress does not emit a phantom
        # inbound update every pass (bug 85a1 family — assignee convergence).
        if local_field == "assignee":
            local_assignee = local_ticket.get(ticket_field) or ""
            if not _assignee_matches(local_assignee, jira_fields.get("assignee")):
                changed[local_field] = jira_mapped.get(local_field)
                # Bug 8d68: forward the CANONICAL identity alongside the scalar. The scalar is
                # a bare string (``_extract_jira_field_value``: ``name``, else ``displayName``)
                # and the applier's ghost mint requires the opaque external id, so with only
                # the scalar the mint at ``apply_inbound_records:374`` returned early and an
                # inbound EDIT never minted — while an inbound CREATE, which is handed the raw
                # user object, always did. ``_map_jira_to_local_fields:225`` already computes
                # this at no extra extraction; ``account_id`` holds the accountId on Cloud and
                # the username on Data Center (``_identity_of:154-179``, 5f48's precedence).
                # Inert for event writing: ``_inbound_update_write_edit_event`` builds its EDIT
                # from an explicit field list, so an extra key writes no extra ticket field.
                if "assignee_identity" in jira_mapped:
                    changed["assignee_identity"] = jira_mapped["assignee_identity"]
            continue
        if local_field == "status" and "status" not in jira_mapped:
            continue  # Bug 5886: unmapped Jira status → leave local status untouched.
        jira_val = jira_mapped.get(local_field)
        local_val = local_ticket.get(ticket_field)
        # Normalise None to empty string for string fields
        if jira_val is None:
            jira_val = "" if local_field not in ("priority",) else 2
        if local_val is None:
            local_val = "" if local_field not in ("priority",) else 2
        # Convergence (mirror of outbound_fields.py:430-435, inbound direction):
        # the send path truncates an over-length description so its ADF
        # representation fits Jira's limit, so the Jira value we fetch back is the
        # truncated form. Apply the IDENTICAL shared ADF-aware fit to the local
        # value before comparing; otherwise an oversized local description never
        # matches the landed truncated Jira body and the differ pulls the
        # truncated form back into the local store every pass (clobbering the full
        # local description and invalidating its plan-review fingerprint). The fit
        # is applied to an in-memory comparison copy only — never written into
        # ``changed`` — so a genuine Jira-side edit (Jira != fit(local)) still flows.
        # The same holds for soft-wrap normalization: the ADF encoder rejoins
        # hard-wrapped prose into one paragraph, so the Jira body decodes back
        # JOINED while the local value stays hard-wrapped. Normalize the local
        # comparison copy identically or inbound clobbers the local wrapping. The
        # Jira side is normalized for the COMPARISON only (``jira_emit`` keeps the
        # value actually pulled into ``changed``); in production it is already the
        # decoded — hence normalized — ADF body, so this is a no-op there.
        jira_emit = jira_val
        if local_field == "description" and isinstance(local_val, str):
            forms = _description_forms(
                local_val, jira_val, jira_emit, local_ticket, outbound_mapper
            )
            if forms is None:
                continue
            local_val, jira_val = forms
        # Bug (plateau): trailing-whitespace round-trip stability.
        # Mirror of the outbound_differ fix — Jira's ADF normalization
        # strips trailing whitespace, so a local description ending in
        # ``\n\n`` and Jira's stripped form must compare equal.
        # Without this, inbound emits a description update that would
        # clobber local's user-authored trailing whitespace just because
        # Jira normalized it.
        if (
            isinstance(local_val, str)
            and isinstance(jira_val, str)
            and local_val.rstrip() == jira_val.rstrip()
        ):
            continue
        # ADR 0029 #2 — don't flip a locally-terminal ticket to closed on Jira's Done echo (444d).
        if (
            local_field == "status"
            and local_val in ("archived", "deleted")
            and jira_val == "closed"
        ):
            continue
        if jira_val != local_val:
            changed[local_field] = jira_emit

    # Parent sync (ticket 8b25; three-state + gated CLEAR in ticket 88d9). The decision —
    # observed/unobserved/unresolvable, and the managed-ref gate on a clear — lives in
    # ``inbound_fields.diff_inbound_parent``, which already owns parent-field extraction.
    # Skip when no binding_store provided (legacy call path).
    if binding_store is not None:
        # The CLEAR needs positive evidence the peer once had this parent (ticket 88d9's
        # orphaning incident). ``get_peer_parent`` is getattr-guarded because legacy stores and
        # test doubles predate it, and its absence must degrade to NO CLEAR, never to a clear.
        _get_peer_parent = getattr(binding_store, "get_peer_parent", None)
        _peer_parent = (
            _get_peer_parent(local_ticket.get("ticket_id") or "")
            if _get_peer_parent is not None
            else None
        )
        emit_parent, parent_value = diff_inbound_parent(
            jira_fields, local_ticket, binding_store.get_local_id, _peer_parent
        )
        if emit_parent:
            changed["parent_id"] = parent_value

    return changed


# ---------------------------------------------------------------------------
# Label diff helpers
# ---------------------------------------------------------------------------

# Bug eadb (Issue A): the colon-form ``rebar-id:<local_id>`` label was missing
# from this exclusion list (only the hyphen-form ``rebar-id-<local_id>`` was
# present), so the inbound differ saw the canonical Jira-side rebar-id label
# as a "Jira-only" tag and emitted an inbound ADD on every pass — leaking
# bridge-internal identifiers into local ticket ``tags``. The outbound
# differ's ``_EXCLUDED_PREFIXES`` was patched for the same root cause in
# PR #454; this is the inbound mirror of that fix. Both separator forms
# must be excluded: ``rebar-id:`` is the canonical form written by
# ``_apply_outbound_create`` / ``_apply_inbound_create``; ``rebar-id-`` is
# preserved for backward compatibility with pre-cutover labels still on
# legacy Jira issues.
# rebar-status: annotation labels are reconciler-managed (emitted/removed by
# status logic); they must not leak into local ticket tags via inbound label
# sync (ticket 929a). Exclude from both sides of the label diff.
_EXCLUDED_PREFIXES: tuple[str, ...] = ("rebar-id:", "rebar-id-", "imported:", "rebar-status:")


def _diff_comments_inbound(
    jira_fields: dict[str, Any], local_ticket: dict[str, Any]
) -> list[dict[str, Any]]:
    """Detect Jira-side comments not yet mirrored locally (bug 85a1, Gap 1).

    Snapshot lookup: the Jira REST API nests comments at
    ``fields["comment"]["comments"]`` (outer key is the SINGULAR ``"comment"``,
    not ``"comments"``). The fetcher enriches each snapshot entry with this
    nested ``comment`` field verbatim, so we read
    ``jira_fields["comment"]["comments"]`` — mirroring the outbound differ
    (:func:`outbound_differ._diff_comments`). When the ``comment`` key is
    absent (no comment data this pass — e.g. the live-search snapshot shape),
    there are no inbound comment mutations; when present but malformed, we
    treat it as no comments.

    Strategy (validated against live Jira during development):
      1. Read each Jira comment's id + body.
      2. Loop-breaker: skip any comment whose body contains
         ``RECONCILER_MARKER`` — that's our own outbound echo.
      3. Set-diff: skip any Jira comment whose id matches a local
         comment's ``jira_comment_id`` field (already mirrored).
      4. For each remaining Jira comment, emit an "add" mutation with
         the normalised plain-text body and the source jira_comment_id
         so the applier can write the binding back when persisting locally.

    Returns: list of dicts ``{"action": "add", "body": ..., "jira_comment_id": ...}``.
    The applier consumes this list when writing inbound updates to the
    local tickets-tracker.
    """
    # Jira REST nests comments under the singular "comment" key as
    # {"comments": [...], "total": N}. Key absent → no comment data this pass.
    comment_field = jira_fields.get("comment")
    if not isinstance(comment_field, dict):
        return []
    jira_comments = comment_field.get("comments") or []
    if not isinstance(jira_comments, list):
        return []

    known_ids: set[str] = set()
    for lc in local_ticket.get("comments") or []:
        if isinstance(lc, dict):
            jid = lc.get("jira_comment_id")
            if jid is not None:
                known_ids.add(str(jid))

    mutations: list[dict[str, Any]] = []
    for jc in jira_comments:
        if not isinstance(jc, dict):
            continue
        jid = jc.get("id")
        if jid is None:
            continue
        jid_str = str(jid)
        if jid_str in known_ids:
            continue  # already mirrored locally

        body_text = _normalize_jira_body(jc.get("body"))
        if RECONCILER_MARKER in body_text:
            continue  # outbound echo — do not pull our own comment back in
        if not body_text.strip():
            continue

        mutations.append(
            {
                "action": "add",
                "body": body_text,
                "jira_comment_id": jid_str,
            }
        )
    return mutations


# Link diff (story 25ae, Cycle 2). The Jira-issuelink -> rebar-relation DIRECTION
# logic (resolve_inbound_link, deps_as_set, INVERSE_RELATION) lives in the sibling
# ``link_direction`` module — one source of truth shared with the REMOVE path
# (outbound_links), pinned to live-Jira ground truth by test_link_direction_absolute.py
# (bug 4b59). Loaded via _load_link_direction() to keep standalone test imports working.


def _diff_links_inbound(
    jira_fields: dict[str, Any],
    local_ticket: dict[str, Any],
    binding_store: Any,
    local_tickets_by_id: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Reflect Jira issuelinks into rebar relations. ADD-only.

    Direction semantics: delegated to ``link_direction.resolve_inbound_link``
    (pinned to captured live-Jira ground truth) — ``outwardIssue`` Blocks ->
    ``blocks``, ``inwardIssue`` Blocks -> ``depends_on``.

    Idempotency — INVERSE-AWARE, CROSS-TICKET dedup (bug 4b59). rebar stores each
    blocking edge ONCE, one-directionally; Jira shows it from BOTH endpoints, so a
    naive per-ticket dedup re-emits the counterpart's edge as a spurious *mirror*
    (~400 store-wide). Skip an emit when this ticket already carries ``(relation,
    target)``; OR the COUNTERPART carries the inverse edge ``(inverse, this-ticket)``;
    OR the counterpart is absent from the ACTIVE local set (``local_tickets_by_id`` is
    built from ``rebar list --full``, EXCLUDING archived/deleted — a mirror there is
    un-verifiable). Live-validated: converges to 0 emits for already-synced links.
    ``local_tickets_by_id`` is optional (legacy/unit callers get direction-only dedup);
    production MUST pass it. ADD-only (no REMOVE mutations).
    """
    issuelinks = jira_fields.get("issuelinks") or []
    if not isinstance(issuelinks, list):
        return []

    ld = _load_link_direction()
    this_id = local_ticket.get("ticket_id")
    existing_deps = ld.deps_as_set(local_ticket)

    mutations: list[dict[str, Any]] = []
    emitted: set[tuple[str, str]] = set()
    for link in issuelinks:
        if not isinstance(link, dict):
            continue
        other_key, relation = ld.resolve_inbound_link(link)
        if other_key is None or relation is None:
            continue  # unmapped link type / malformed entry

        target_id = binding_store.get_local_id(other_key)
        if not target_id:
            continue  # unbound — retry next pass
        key = (relation, target_id)
        if key in existing_deps or key in emitted:
            continue  # this ticket already carries the dep — no churn

        if local_tickets_by_id is not None:
            counterpart = local_tickets_by_id.get(target_id)
            if counterpart is None:
                continue  # counterpart archived/deleted/unbound — don't mirror to a dormant ticket
            inverse = ld.INVERSE_RELATION.get(relation, relation)
            if (inverse, this_id) in ld.deps_as_set(counterpart):
                continue  # counterpart already owns this edge (inverse form) — no mirror

        emitted.add(key)
        mutations.append({"action": "add", "target_id": target_id, "relation": relation})
    return mutations


def _diff_link_removals_inbound(
    jira_fields: dict[str, Any],
    local_ticket: dict[str, Any],
    binding_store: Any,
) -> list[dict[str, Any]]:
    """Mirror a peer link DELETION locally — the REMOVE half of the link diff (ticket 2b16).

    The inbound mirror of ``outbound_links._diff_link_removals``: walks the LOCAL deps and
    emits ``{"action": "remove", "target_id": <local id>, "relation": <local relation>}`` per
    dep whose peer counterpart is gone (inbound records carry a local ``target_id``, outbound a
    peer ``to_key``). This WRITE DESTROYS LOCAL DATA and the enrichment it reads is fail-open,
    so three guards stand between "not in the snapshot" and a delete:

    * **G1 OBSERVED** — ``"issuelinks" in jira_fields``: key PRESENT, empty list allowed. The
      fetcher sets it only for issues ``get_issuelinks_map`` returned a list for, and the base
      search omits issuelinks entirely (bug 3f04), so presence means "enrichment reached this
      issue" and ``[]`` is an authoritative "no links". Key-ABSENT is exactly the unsafe set
      (failed enrichment, HTTP 410, a partial fail-open map, a truncated page, a cross-project
      issue, no ``get_issuelinks_map``) and must fail safe. ``_diff_links_inbound`` opens
      ``... .get("issuelinks") or []``, COLLAPSING observed-empty into unobserved — harmless
      when ADD-only, never reusable here.
    * **G2 TARGET BOUND** — an unbound target could never have carried a peer link.
    * **G3 MANAGED** — ``should_propagate_removal``, the gate the outbound parent/link paths
      already consume, so a peer-created dep rebar never owned is not clobbered; it excludes
      ``duplicates``/``supersedes``/``discovered_from`` for free (``MANAGED_REF_KINDS`` omits
      relations with no peer link type) — add no redundant filter. BLIND SPOT: a ref is managed
      the moment it is created LOCALLY (``add_managed_ref`` is folded by the LINK-event
      processor), which is not "we pushed it". G4 (the action-aware same-pass suppression in
      ``compute_inbound_mutations``) closes that; G5 (the relation match in
      ``apply_inbound_records``) guards the pair-scoped ``rebar.unlink``.

    Both sides compare in LOCAL RELATION vocabulary via ``link_direction.observed_peer_deps``
    (see there for why raw peer keys cannot be used). Deliberately does NOT reuse
    ``_diff_links_inbound``'s dormant-counterpart guard — a removal whose counterpart is
    archived or locally deleted must still be mirrored, not skipped forever.
    """
    if "issuelinks" not in jira_fields:
        return []  # G1: unobserved — never infer a deletion from data we did not fetch
    issuelinks = jira_fields["issuelinks"]
    if not isinstance(issuelinks, list):
        return []  # G1: a malformed value is not an observation either
    get_jira_key = getattr(binding_store, "get_jira_key", None)
    if get_jira_key is None:
        return []  # cannot establish G2 — fail safe

    from rebar.reducer._managed_refs import should_propagate_removal

    ld = _load_link_direction()
    observed = ld.observed_peer_deps(issuelinks, binding_store.get_local_id)

    removals: list[dict[str, Any]] = []
    for relation, target_id in sorted(ld.deps_as_set(local_ticket)):
        if (relation, target_id) in observed:
            continue  # still on the peer — steady state, no churn
        if not get_jira_key(target_id):
            continue  # G2
        if not should_propagate_removal(relation, target_id, local_ticket):
            continue  # G3
        removals.append({"action": "remove", "target_id": target_id, "relation": relation})
    return removals


def _diff_labels_inbound(
    jira_fields: dict[str, Any], local_ticket: dict[str, Any]
) -> list[dict[str, Any]]:
    """Compare Jira labels to local tags. Exclude bridge-internal labels."""
    jira_labels: set[str] = set(
        label
        for label in (jira_fields.get("labels") or [])
        if not any(label.startswith(p) for p in _EXCLUDED_PREFIXES)
    )
    local_tags: set[str] = set(
        t
        for t in local_ticket.get("tags", [])
        if not any(t.startswith(p) for p in _EXCLUDED_PREFIXES)
    )

    mutations: list[dict[str, Any]] = []
    for label in sorted(jira_labels - local_tags):
        mutations.append({"action": "add", "label": label})
    for label in sorted(local_tags - jira_labels):
        mutations.append({"action": "remove", "label": label})
    return mutations


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


# CONTRACT — cross-direction field-name canonicalization
# -------------------------------------------------------
# Maps an outbound (Jira REST) field name to its inbound (local ticket) name
# so that bidirectional suppression in ``_build_outbound_context`` compares
# like-named fields regardless of which side emitted the mutation.
#
# Bug 8b25 root cause: ``outbound_differ`` emits ``parent`` (the Jira REST
# field); ``inbound_differ`` emits ``parent_id`` (the local ticket field).
# Without this map the scalar-field suppression set never matched, causing the
# two differs to oscillate every pass against a stale pre-pass Jira snapshot —
# observable as perpetual ``fields=['parent']`` churn and Phase-2c parent FAIL
# in the e2e probe.
#
# Canonical entries (as of 183fd51ac2; pending consolidation into
# _field_contract.py per docs/designs/sync-hardening-proposal.md Item 3):
#   ``parent``  → ``parent_id``  (Jira REST name → local ticket field name)
#   ``summary`` → ``title``      (Jira REST name → local ticket field name)
#
# Bug 0702-3b6d-c1db-4ed3: ``summary``→``title`` was MISSING. The outbound
# differ emits the title change under the Jira REST field name ``summary``
# (see ``outbound_differ._map_local_to_jira_fields``), while the inbound differ
# emits the same logical field under the LOCAL name ``title``. Without this
# entry the scalar-field suppression set never matched for the title field, so
# an outbound title push did NOT suppress the inbound re-emission of the stale
# Jira title — the two differs oscillated on the title every pass. This was
# latent until the inbound bound-but-absent fix made out-of-window keys
# inbound-visible (where local-edited-but-Jira-stale title is the common case).
#
# MAINTENANCE RULE: any field that the outbound differ can emit under a
# DIFFERENT name than the inbound differ MUST add an entry here.  Fields
# whose name is identical in both directions do NOT need an entry.  When
# adding a new bidirectional field, update this map and add a corresponding
# assertion in tests/unit/rebar_reconciler/test_inbound_differ_field_contract.py.
_OUTBOUND_TO_INBOUND_FIELD: dict[str, str] = {
    "parent": "parent_id",
    "summary": "title",
}


def _build_outbound_context(
    outbound_mutations: list[Any] | None,
) -> dict[str, dict[str, Any]]:
    """Index outbound mutations by jira_key for fast lookup.

    Bug 3bf8: in a single bidirectional pass, outbound and inbound differs
    run against the same pre-pass snapshot. When the local side has just
    diverged from Jira, the inbound differ would naively emit a mutation
    that reverts the local-side change. This index lets the inbound differ
    detect and suppress those contradictions.

    Returns a dict keyed by jira_key with:
      - "label_adds": set of labels being added outbound
      - "label_removes": set of labels being removed outbound
      - "fields": set of field names being updated outbound
      - "link_add_keys": set of target Jira keys being link-added outbound
        (story 25ae Cycle 2 — echo-suppression for link adds: an outbound
        link push for key Y must not be re-reflected inbound the same pass)
    """
    ctx: dict[str, dict[str, Any]] = {}
    if not outbound_mutations:
        return ctx
    for om in outbound_mutations:
        jira_key = getattr(om, "jira_key", None)
        if not jira_key:
            continue
        entry = ctx.setdefault(
            jira_key,
            {
                "label_adds": set(),
                "label_removes": set(),
                "fields": set(),
                "link_add_keys": set(),
                "link_remove_keys": set(),
            },
        )
        for lk in getattr(om, "links", []) or []:
            if not isinstance(lk, dict):
                continue
            action = lk.get("action")
            to_key = lk.get("to_key")
            if action == "add" and to_key:
                entry["link_add_keys"].add(to_key)
            elif action == "remove" and to_key:
                # wake-inn-parse: an outbound link REMOVE (a deliberate local unlink) must
                # suppress the inbound link ADD that would re-reflect the still-present Jira
                # link this pass — remove-wins, so local wins and the unlink converges.
                entry["link_remove_keys"].add(to_key)
        for lm in getattr(om, "labels", []) or []:
            action = lm.get("action") if isinstance(lm, dict) else None
            label = lm.get("label") if isinstance(lm, dict) else None
            if not label:
                continue
            if action == "add":
                entry["label_adds"].add(label)
            elif action == "remove":
                entry["label_removes"].add(label)
        for field_name in (getattr(om, "fields", {}) or {}).keys():
            # Bug 8b25: the outbound differ emits the parent under the Jira
            # field name ``parent`` (a bare key string), while the inbound
            # differ emits the same logical change under the LOCAL field name
            # ``parent_id``. Record the inbound-side name so the scalar
            # suppression at the call site (which keys on inbound field names)
            # actually matches. Without this canonicalisation, an outbound
            # ``parent`` reparent never suppresses the inbound ``parent_id``
            # re-emission, and the two differs oscillate every pass against a
            # stale pre-pass Jira snapshot — the perpetual ``fields=['parent']``
            # steady-state churn and the e2e probe's parent FAIL.
            entry["fields"].add(_OUTBOUND_TO_INBOUND_FIELD.get(field_name, field_name))
    return ctx


def _resolve_backend_mappers(
    inbound_mapper: InboundMapper | None,
    outbound_mapper: OutboundMapper | None,
) -> tuple[InboundMapper, OutboundMapper]:
    """Resolve the injected Backend-port field mappers, falling back to the configured
    backend via the neutral registry seam (ticket 4af8 / 3289). The orchestrator injects
    both; a direct caller (unit test) may omit either. Resolving the backend once here keeps
    the differ free of any vendor mapper reference and yields a consistent pair."""
    if inbound_mapper is not None and outbound_mapper is not None:
        return inbound_mapper, outbound_mapper
    from rebar.config import compose_config
    from rebar_reconciler._backend_registry import select_backend

    backend = select_backend(compose_config())
    return (inbound_mapper or backend.inbound), (outbound_mapper or backend.outbound)


def compute_inbound_mutations(
    jira_snapshot: dict[str, dict[str, Any]],
    binding_store: BindingStoreProtocol,
    local_tickets_by_id: dict[str, dict[str, Any]],
    outbound_mutations: list[Any] | None = None,
    *,
    inbound_mapper: InboundMapper | None = None,
    outbound_mapper: OutboundMapper | None = None,
) -> tuple[list[InboundMutation], int]:
    """Detect Jira-side changes for bound tickets.

    Only processes BOUND tickets (those in binding_store). Unbound Jira issues
    are ignored — they will be handled by outbound creates once the local
    ticket is synced outbound first.

    For bound tickets where Jira fields differ from local:
    - If local also changed -> skip (local wins, outbound differ handles it)
    - If only Jira changed -> emit inbound update

    Bidirectional coordination (bug 3bf8): when ``outbound_mutations`` is
    provided, the inbound differ filters its emissions to suppress any
    mutation that contradicts an outbound mutation just emitted for the
    same target. The local-side change is fresher than the differ
    snapshot, so it has authoritative priority for the pass:
      - label ADD suppressed when outbound is REMOVING the same label
      - label REMOVE suppressed when outbound is ADDING the same label
      - scalar field update suppressed when outbound is updating the same field
    The next pass converges both sides without a phase-2 snapshot refresh.

    Args:
        jira_snapshot: Dict of {jira_key: {fields...}} from the fetcher.
        binding_store: A BindingStore instance providing get_local_id(jira_key).
        local_tickets_by_id: Dict of {local_id: {ticket fields...}} for local
            ticket lookup.
        outbound_mutations: Optional list of OutboundMutation objects emitted
            in the same pass; used to suppress inbound mutations that would
            contradict an outbound change for the same target. Defaults to
            None (no coordination — legacy behaviour preserved).

    Returns:
        Tuple of ``(mutations, suppression_count)``:
          - ``mutations``: list of InboundMutation objects describing changes
            to apply locally (post-suppression).
          - ``suppression_count``: integer count of inbound field- and
            label-level items that were dropped by bidirectional
            suppression in this call. Zero when ``outbound_mutations`` is
            None or empty. Used by reconcile telemetry to emit the
            ``RECON: bidir_suppressed`` line without a second pass.
    """
    # Ticket 4af8 / 3289: the remote->local and local->remote field mappers are injected
    # via the Backend port (run_differs passes ``backend.inbound`` / ``backend.outbound``);
    # a direct caller (unit test) that omits either resolves it through the neutral registry
    # seam, so this differ names no vendor mapper.
    inbound_mapper, outbound_mapper = _resolve_backend_mappers(inbound_mapper, outbound_mapper)

    mutations: list[InboundMutation] = []
    outbound_ctx = _build_outbound_context(outbound_mutations)
    suppression_count = 0

    for jira_key, jira_fields in sorted(jira_snapshot.items()):
        local_id = binding_store.get_local_id(jira_key)
        if local_id is None:
            # Unbound Jira issue — skip (local is source of truth)
            continue

        local_ticket = local_tickets_by_id.get(local_id)
        if local_ticket is None:
            # Bound but local ticket missing — skip (may be deleted locally)
            continue

        changed = _diff_jira_vs_local(
            jira_fields,
            local_ticket,
            binding_store=binding_store,
            inbound_mapper=inbound_mapper,
            outbound_mapper=outbound_mapper,
        )
        label_mutations = _diff_labels_inbound(jira_fields, local_ticket)
        comment_mutations = _diff_comments_inbound(jira_fields, local_ticket)
        link_mutations = _diff_links_inbound(
            jira_fields, local_ticket, binding_store, local_tickets_by_id
        )
        # Ticket 2b16: the symmetric REMOVE half — a local dep whose peer link was deleted.
        # Guarded (G1 observed / G2 bound / G3 managed); see _diff_link_removals_inbound.
        link_mutations.extend(_diff_link_removals_inbound(jira_fields, local_ticket, binding_store))

        # Bidirectional suppression (bug 3bf8): filter out inbound mutations
        # that would clobber a just-emitted outbound change for this target.
        ob_entry = outbound_ctx.get(jira_key)
        if ob_entry is not None:
            # Scalar fields: drop any inbound field update where outbound
            # is updating the same field.
            if changed:
                pre_field_count = len(changed)
                changed = {k: v for k, v in changed.items() if k not in ob_entry["fields"]}
                suppression_count += pre_field_count - len(changed)
            # Labels: drop inbound ADD when outbound REMOVES the same label,
            # and inbound REMOVE when outbound ADDS the same label.
            if label_mutations:
                filtered_labels: list[dict[str, Any]] = []
                for lm in label_mutations:
                    action = lm.get("action")
                    label = lm.get("label")
                    if action == "add" and label in ob_entry["label_removes"]:
                        suppression_count += 1
                        continue
                    if action == "remove" and label in ob_entry["label_adds"]:
                        suppression_count += 1
                        continue
                    filtered_labels.append(lm)
                label_mutations = filtered_labels
            # Links: same-pass precedence, ACTION-AWARE (G4, ticket 2b16). The inbound record
            # carries the LOCAL target_id; map it back to the peer key to compare against the
            # outbound key sets. This filter used to test the key WITHOUT inspecting ``action``,
            # so it would have suppressed an inbound REMOVE only by accident. The actions are
            # listed apart because they are suppressed for DIFFERENT reasons (sets coincide).
            if link_mutations and (ob_entry["link_add_keys"] or ob_entry["link_remove_keys"]):
                _get_jira_key = getattr(binding_store, "get_jira_key", None)
                _suppress_by_action: dict[str, set[str]] = {
                    # An inbound ADD is either an echo of our own push (outbound ADD, bug
                    # 3bf8) or contradicts a deliberate local unlink (outbound REMOVE —
                    # remove-wins so local wins, bug wake-inn-parse). Drop it either way.
                    "add": ob_entry["link_add_keys"] | ob_entry["link_remove_keys"],
                    # G4 — THE GUARD THAT COVERS G3's BLIND SPOT, not a cosmetic one. A ref is
                    # "managed" the instant it is created LOCALLY (add_managed_ref is folded by
                    # the LINK-event processor), so G3 cannot tell a brand-new local dep from a
                    # peer-deleted one, and the peer legitimately has no such link yet. In a
                    # healthy pass outbound emits a link ADD for exactly that dep, so an
                    # outbound ADD to this target means "being pushed", NOT "peer deleted it" —
                    # never mirror a removal for it. An outbound REMOVE for the same pair also
                    # suppresses: the two directions must not both act on one pair in a single
                    # pass, and deferring re-evaluates next pass rather than double-removing.
                    "remove": ob_entry["link_add_keys"] | ob_entry["link_remove_keys"],
                }
                filtered_links: list[dict[str, Any]] = []
                for lk in link_mutations:
                    target_key = (
                        _get_jira_key(lk.get("target_id")) if _get_jira_key is not None else None
                    )
                    _keys = _suppress_by_action.get(str(lk.get("action")), set())
                    if target_key and target_key in _keys:
                        suppression_count += 1
                        continue
                    filtered_links.append(lk)
                link_mutations = filtered_links

        if changed or label_mutations or comment_mutations or link_mutations:
            mutations.append(
                InboundMutation(
                    jira_key=jira_key,
                    local_id=local_id,
                    action="update",
                    fields=changed,
                    labels=label_mutations,
                    comments=comment_mutations,
                    links=link_mutations,
                )
            )

    return mutations, suppression_count
