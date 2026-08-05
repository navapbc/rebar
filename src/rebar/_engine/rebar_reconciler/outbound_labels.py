"""Outbound label-diff cluster for bidirectional Jira sync.

Extracted verbatim from ``outbound_differ`` along the seam that already existed
there — a self-contained cluster (``_diff_labels`` + ``_diff_status_annotation_labels``
and the two constants they read) that the differ calls but nothing else in the module
feeds. Splitting it keeps ``outbound_differ`` under the repo's module-size cap and puts
these next to their siblings ``outbound_links`` / ``outbound_comments`` /
``outbound_assignee``, which already own the other per-concern outbound diffs.

Behaviour is unchanged; ``outbound_differ`` re-exports these names so existing
``outbound_differ.<name>`` references keep resolving.
"""

from __future__ import annotations

from typing import Any

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
# Status annotation label helpers (ticket 929a)
# ---------------------------------------------------------------------------

# Local statuses that need an annotation label to preserve lossless intent.
# Maps local_status -> rebar-status:<label> emitted when that status is active.
# (blocked/cancelled have no direct equivalent in the live DIG workflow, so the
# nearest live state plus this annotation label is the lossless encoding.)
_STATUS_ANNOTATION_LABEL: dict[str, str] = {
    "blocked": "rebar-status:blocked",
    "cancelled": "rebar-status:cancelled",
}


def _diff_status_annotation_labels(
    local_status: str,
    jira_labels: list[str],
) -> list[dict[str, Any]]:
    """Compute add/remove mutations for rebar-status: annotation labels.

    These labels encode lossless status information for statuses that have no
    direct equivalent in the live DIG Jira workflow (currently blocked and
    cancelled, which map to In Progress and Done respectively).

    Rules:
    - When local_status is in _STATUS_ANNOTATION_LABEL, emit ADD for the
      corresponding rebar-status: label if Jira does not already carry it.
    - When a rebar-status: annotation label is present on Jira but local_status
      no longer matches it, emit REMOVE to clean up the stale label.
    """
    mutations: list[dict[str, Any]] = []
    desired_annotation = _STATUS_ANNOTATION_LABEL.get(local_status)
    jira_annotation_labels = {label for label in jira_labels if label.startswith("rebar-status:")}

    # Add desired annotation if not already present
    if desired_annotation is not None and desired_annotation not in jira_annotation_labels:
        mutations.append({"action": "add", "label": desired_annotation})

    # Remove stale annotations (rebar-status: labels that no longer match)
    for stale in sorted(jira_annotation_labels):
        if stale != desired_annotation:
            mutations.append({"action": "remove", "label": stale})

    return mutations
