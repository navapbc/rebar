"""Collection-valued inbound diffs for bidirectional Jira sync.

The half of the inbound differ that fills ``InboundMutation.comments`` / ``.labels``
/ ``.links``: one diff helper per collection-valued facet of a bound ticket, each
returning a list of mutation RECORDS rather than the scalar field map its sibling
``_diff_jira_vs_local`` produces.

Extracted from ``inbound_differ`` along the seam that module's call graph already
had (story 64ae-262f-990a-49ae): every helper here is called from exactly one place,
``inbound_differ.compute_inbound_mutations``, and the private helpers they share
between them — ``_load_link_direction``, ``RECONCILER_MARKER``,
``_EXCLUDED_PREFIXES`` — are used by nothing else, so the extracted set is closed.
The sibling keeps the scalar-field diff and the pass orchestration.

Every name here is re-exported from ``inbound_differ``, so ``inbound_differ.<symbol>``
attribute access (including the by-path module loads the reconciler tests use)
keeps resolving unchanged.

This module is pure: no I/O, no time/random, no logging, and no global state beyond
the lazily-cached ``link_direction`` module handle.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

# ``_normalize_jira_body`` is the Jira->plain-text body normalizer owned by the
# ``inbound_fields`` translation leaf; the comment diff is its only consumer here.
from rebar_reconciler.inbound_fields import _normalize_jira_body

# Reconciler loop-breaker marker (Gap 1). Outbound comments embed this
# token; inbound passes filter any Jira comment whose body contains it
# so we do not detect our own echoes as new Jira-side comments.
RECONCILER_MARKER = "<!-- rebar:reconciler-echo -->"


_LINK_DIR_KEY = "rebar_reconciler.link_direction"
_LinkDirModule = None


def _load_link_direction():
    """Lazy-load the sibling link_direction module.

    Mirrors the by-path lazy loader ``inbound_fields`` uses for its rich-text codec:
    keeping the import off module scope is what lets these leaves be loaded standalone
    by path, the convention the reconciler tests rely on. (Named by description rather
    than by symbol so this module carries no ADF-entry-point token — see the allowlist
    in tests/unit/rebar_reconciler/test_rich_text_seam_heldout.py.)
    """
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
