"""Pure inbound differences for comments, labels, and links on bound tickets.

Each helper returns mutation records for compute_inbound_mutations. The scalar
field differ remains in inbound_differ, which re-exports these helpers."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from typing import Any

# ``_normalize_jira_body`` is the Jira->plain-text body normalizer owned by the
# ``inbound_fields`` translation leaf; the comment diff is its only consumer here.
from rebar.reconciler.inbound_fields import _normalize_jira_body

# Reconciler loop-breaker marker (Gap 1). Outbound comments embed this
# token; inbound passes filter any Jira comment whose body contains it
# so we do not detect our own echoes as new Jira-side comments.
RECONCILER_MARKER = "<!-- rebar:reconciler-echo -->"


_LINK_DIR_KEY = "rebar.reconciler.link_direction"
_LinkDirModule = None


def _load_link_direction():
    """Lazy-load the sibling link_direction module.

    Mirrors the by-path lazy loader ``inbound_fields`` uses for its rich-text codec:
    keeping the import off module scope is what lets these leaves be loaded standalone
    by path, the convention the reconciler tests rely on. (Named by description rather
    than by symbol so this module carries no ADF-entry-point token — see the allowlist
    in tests/unit/reconciler/test_rich_text_seam_heldout.py.)
    """
    global _LinkDirModule
    if _LinkDirModule is not None:
        return _LinkDirModule
    if _LINK_DIR_KEY in sys.modules:
        _LinkDirModule = sys.modules[_LINK_DIR_KEY]
        return _LinkDirModule
    mod = importlib.import_module("rebar.reconciler.link_direction")
    _LinkDirModule = mod
    return mod


# Exclude canonical and legacy rebar-id labels plus status annotations from
# local tags. They are bridge-owned identity and status metadata.
_EXCLUDED_PREFIXES: tuple[str, ...] = (
    "rebar-id:",
    "rebar-id-",
    "imported:",
    "rebar-status:",
    "rebar-type:",
)


def _diff_comments_inbound(
    jira_fields: dict[str, Any], local_ticket: dict[str, Any]
) -> list[dict[str, Any]]:
    """Read Jira comments from the singular comment.comments snapshot field.
    Absent or malformed comment data emits nothing. Skip bridge-marked echoes and
    IDs already present locally. Emit each remaining plain-text add with its Jira
    comment ID for local persistence."""
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


# Load shared link-direction rules for inbound adds and outbound removals.
# The loader keeps standalone module imports working.


def _diff_links_inbound(
    jira_fields: dict[str, Any],
    local_ticket: dict[str, Any],
    binding_store: Any,
    local_tickets_by_id: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Add Jira links using shared direction rules. Skip an edge already stored on
    this ticket or as the inverse on its active counterpart. A missing active
    counterpart cannot prove deduplication, so do not emit its mirror."""
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
    """Remove a local managed link only after Jira link enrichment was observed for
    this issue, even when the observed list is empty. Require a bound target and
    should_propagate_removal before emitting a local-relation removal. Missing or
    malformed issuelinks data cannot prove deletion. Unlike add deduplication,
    archived counterparts do not suppress a confirmed removal."""
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
