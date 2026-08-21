"""Outbound label-diff cluster for bidirectional Jira sync.

Extracted verbatim from ``outbound_differ`` along the seam that already existed
there — a self-contained cluster (``_diff_labels`` + ``_diff_status_annotation_labels``
and the two constants they read) that the differ calls but nothing else in the module
feeds. Splitting it keeps ``outbound_differ`` under the module-size cap (see ADR 0058)
and puts
these next to their siblings ``outbound_links`` / ``outbound_comments`` /
``outbound_assignee``, which already own the other per-concern outbound diffs.

Behaviour is unchanged; ``outbound_differ`` re-exports these names so existing
``outbound_differ.<name>`` references keep resolving.
"""

from __future__ import annotations

from typing import Any

from rebar_reconciler import config

# ---------------------------------------------------------------------------
# Label diff
# ---------------------------------------------------------------------------

# NOTE: applier.py writes the bridge-internal binding label as
# f"rebar-id:{local_id}" (COLON separator). Legacy code paths used a HYPHEN
# separator ("rebar-id-<local_id>"); both forms must be excluded from outbound
# diffs to avoid emitting spurious remove mutations for identity labels.
# See bug 68a4-f9d5-5540-4b95.
# rebar-status: labels are reconciler-managed annotation labels (emitted/removed
# by status logic only); they must be excluded from the normal user-tag diff
# so that rebar-status: labels on Jira do not produce spurious REMOVE mutations
# via the tag diff path (ticket 929a).
_EXCLUDED_PREFIXES: tuple[str, ...] = ("rebar-id:", "rebar-id-", "imported:", "rebar-status:")


def _diff_labels(
    ticket: dict[str, Any],
    jira_fields: dict[str, Any],
    intent_set: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Compare local tags to Jira labels. Exclude bridge-internal labels.

    Bug a06c — REMOVE intent gating: when ``intent_set`` is provided
    (non-None), a label in ``jira_labels - local_tags`` only produces a
    REMOVE mutation when it appears in ``intent_set`` (the local
    "ever-seen" set computed by ``local_label_intent``). This prevents
    spurious REMOVEs for labels Jira side-added that local never had —
    the root cause of T3 IB-ADD silently dropping under PR #457 bidir
    suppression.

    When ``intent_set`` is None, the legacy "remove anything in jira
    but not in local" behavior is preserved (backwards compatible for
    every existing test and call site).
    """
    local_tags: set[str] = set(
        t for t in ticket.get("tags", []) if not any(t.startswith(p) for p in _EXCLUDED_PREFIXES)
    )
    jira_labels: set[str] = set(
        label
        for label in (jira_fields.get("labels") or [])
        if not any(label.startswith(p) for p in _EXCLUDED_PREFIXES)
    )

    mutations: list[dict[str, Any]] = []
    for label in sorted(local_tags - jira_labels):
        if intent_set is not None and label not in intent_set:
            # Label is in local's current tag set but was never user-added
            # (only inbound-applied). Suppress the outbound ADD so a
            # subsequent Jira-side REMOVE is not cancelled by a spurious
            # re-ADD (T4 IB-REMOVE regression). See bug a06c.
            continue
        mutations.append({"action": "add", "label": label})
    for label in sorted(jira_labels - local_tags):
        if intent_set is not None and label not in intent_set:
            # Label was never in local's history -> suppress spurious REMOVE.
            continue
        mutations.append({"action": "remove", "label": label})
    return mutations


# ---------------------------------------------------------------------------
# Status annotation label helpers (ticket 929a; generalised S2)
# ---------------------------------------------------------------------------

_REBAR_STATUS_LABEL_PREFIX = "rebar-status:"


def _desired_status_annotation(local_status: str, status_map: dict[str, str] | None) -> str | None:
    """The ``rebar-status:<local_status>`` label to stamp for ``local_status``, or None.

    Built-in-reverse stamp rule (S2, generalising ticket 929a's blocked/cancelled
    literal): a label is needed IFF the local status has a Jira target AND that target
    does NOT reverse-map (via the built-in ``config.jira_to_local_status``) back to the
    local status — i.e. the forward mapping is lossy and the raw workflow status alone
    could not reconstruct the local status inbound.

    ``status_map`` is the effective per-project forward map
    (``config.effective_status_map``); ``None`` falls back to the built-in
    ``config.local_to_jira_status``. A DRIFTED status (no target) stamps NOTHING (the
    lookup is guarded — never blindly subscripted). On the built-in map the lossy set is
    {blocked, cancelled} (``deleted`` is also lossy — "Done" reverses to ``closed`` — but
    ``deleted`` tickets are excluded upstream by ``compute_outbound_mutations`` and never
    reach this rule)."""
    forward = config.local_to_jira_status if status_map is None else status_map
    target = forward.get(local_status)
    if target is None:
        return None
    if config.jira_to_local_status.get(target) == local_status:
        return None
    return f"{_REBAR_STATUS_LABEL_PREFIX}{local_status}"


def _diff_status_annotation_labels(
    local_status: str,
    jira_labels: list[str],
    status_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Compute add/remove mutations for rebar-status: annotation labels.

    These labels encode lossless status information for statuses whose forward mapping
    is lossy (the target reverse-maps to a DIFFERENT local status). On the live DIG
    workflow with the built-in map that is blocked (-> In Progress) and cancelled
    (-> Done); a per-project ``status_map`` generalises the set.

    Rules:
    - Emit ADD for the desired ``rebar-status:<local>`` label (per the built-in-reverse
      stamp rule) when Jira does not already carry it.
    - When a rebar-status: annotation label is present on Jira but no longer matches the
      desired one, emit REMOVE to clean up the stale label.
    """
    mutations: list[dict[str, Any]] = []
    desired_annotation = _desired_status_annotation(local_status, status_map)
    jira_annotation_labels = {
        label for label in jira_labels if label.startswith(_REBAR_STATUS_LABEL_PREFIX)
    }

    # Add desired annotation if not already present
    if desired_annotation is not None and desired_annotation not in jira_annotation_labels:
        mutations.append({"action": "add", "label": desired_annotation})

    # Remove stale annotations (rebar-status: labels that no longer match)
    for stale in sorted(jira_annotation_labels):
        if stale != desired_annotation:
            mutations.append({"action": "remove", "label": stale})

    return mutations
