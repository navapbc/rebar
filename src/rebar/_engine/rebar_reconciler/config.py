"""Configuration constants for rebar_reconciler."""

from __future__ import annotations

EXCLUDED_FIELDS: tuple[str, ...] = ("local_id", "rebar-id")

# Local ticket types that are NEVER synced to Jira. session_log and code_review
# tickets are verbose, local, agent-facing artifacts with no place in a Jira project,
# so compute_outbound_mutations skips them entirely (alongside the excluded-status
# check). These types are also deliberately ABSENT from outbound_differ's
# _LOCAL_TO_JIRA_TYPE map so any leak past this filter surfaces rather than
# silently syncing.
EXCLUDED_SYNC_TYPES: frozenset[str] = frozenset({"session_log", "code_review", "identity"})

# Status mapping: local-side status name -> Jira-side status name.
# Used by outbound_update v1's status-routing path (gated behind
# REBAR_RECONCILER_STATUS_GATING) and by the preflight status-mapping scan
# in reconcile.py — preflight aborts a pass when any update mutation
# references a status absent from this mapping. An empty dict is a valid
# kill-switch — preflight tolerates an empty mapping when no update
# mutations contain a status field.
#
# Must stay in lock-step with adapters/jira_family/value_maps.LOCAL_STATUS_TO_JIRA
# (parity is enforced by tests/unit/rebar_reconciler/state/test_config.py). This is a
# SECOND, INDEPENDENT literal of the same mapping and it is deliberately not an import:
# this module imports nothing but __future__, and adapters/jira_family is a VENDOR
# package whose dependency direction is one-way (concrete backends import it; it never
# imports core back), so importing it here would invert that layering and put this
# operator-overridable surface — including the empty-dict kill-switch above — behind an
# adapter import. The parity TEST is what keeps the two honest instead, exactly as it
# does for jira_to_local_status below. Bug fe15-3bc4-ed70-4b61: before that test, the
# two could drift silently — mutating "deleted" here to a non-workflow value left 59
# tests green.
local_to_jira_status: dict[str, str] = {
    # `idea ↔ IDEA` is a UNIQUE (injective) mapping — no rebar-status: annotation
    # label is needed to reconstruct it inbound. Requires the Jira project workflow
    # to define an `IDEA` status with transitions into/out of it (operator
    # prerequisite — see docs/jira-sync-setup.md "The `idea` status ↔ Jira `IDEA`").
    "idea": "IDEA",
    "open": "To Do",
    "in_progress": "In Progress",
    # blocked/cancelled have no direct equivalent in the live DIG workflow
    # ({To Do, In Progress, In Review, Done} only). Map to the nearest live
    # state; lossless information is preserved via rebar-status: annotation
    # labels emitted/removed by status logic (outbound_differ).
    "blocked": "In Progress",
    "closed": "Done",
    "cancelled": "Done",
    "deleted": "Done",
}

# Canonical reverse mapping: Jira workflow status -> local status. This is
# NOT derivable from local_to_jira_status (the forward map is non-injective:
# blocked/in_progress both map to "In Progress", closed/cancelled/deleted all
# map to "Done"). The canonical preimage is the UNANNOTATED local status —
# blocked/cancelled are reconstructed from rebar-status: annotation labels by
# callers, never from the workflow status alone. Deriving the reverse map by
# inverting local_to_jira_status (as applier._jira_status_to_local once did,
# with lexicographic tie-breaking) imported "In Progress" as blocked and
# "Done" as cancelled — ticket robe-creek-zealot.
#
# Must stay in lock-step with inbound_differ._JIRA_TO_LOCAL_STATUS (parity
# is enforced by tests/unit/rebar_reconciler/test_config.py).
jira_to_local_status: dict[str, str] = {
    "IDEA": "idea",
    "To Do": "open",
    "In Progress": "in_progress",
    # "In Review" is a live DIG workflow state with no local equivalent;
    # nearest local state (matches inbound_differ, ticket 929a).
    "In Review": "in_progress",
    "Blocked": "blocked",
    "Done": "closed",
    "Cancelled": "cancelled",
}


def effective_status_map(project_key: str, root: object = None) -> dict[str, str]:
    """Resolve the EFFECTIVE outbound local->Jira status map for ``project_key``.

    The forward map is ``local_to_jira_status`` (the built-in default) <-
    ``[mapping.default.status_map]`` <- ``[mapping.projects.<KEY>.status_map]``,
    resolved through the S1 per-key three-layer merge (``mapping_config.resolve_for
    _project``). A ``SKIP`` value (``mapping_config.SKIP``) or an absent key means the
    local status has NO Jira target — dropped here so callers see only mappable
    statuses (map-or-drift: a caller that gets no target for a local status OMITS the
    field rather than coercing it).

    With NO ``[mapping]`` block the result equals ``local_to_jira_status`` verbatim —
    the config seam is inert until configured.

    ``mapping_config`` (and, through it, ``rebar.config``) is imported LAZILY so this
    module stays stdlib-only at import time (it imports nothing but ``__future__`` at
    the top level — the operator-overridable status literals above must never sit
    behind an adapter/config import)."""
    from rebar_reconciler import mapping_config as mc

    cfg = mc.load_mapping_config(root)
    builtin = mc.MappingLayer(status_map=local_to_jira_status)
    resolved = mc.resolve_for_project(cfg, project_key, builtin=builtin)
    return {k: v for k, v in resolved.status_map.items() if v != mc.SKIP}


# Local ticket type -> Jira issue type NAME (S3). Like ``local_to_jira_status`` above,
# this is a SECOND, INDEPENDENT literal of the same mapping (the SOLE definition site
# under ``adapters/`` is ``adapters/jira_family/value_maps.LOCAL_TYPE_TO_JIRA``) and it is
# deliberately NOT an import: this module imports nothing but ``__future__``, and
# ``adapters/jira_family`` is a VENDOR package whose dependency direction is one-way
# (concrete backends import it; it never imports core back), so importing it here would
# invert that layering and put this operator-overridable surface behind an adapter import
# — the import-graph contract test enforces exactly this. The parity TEST keeps the two
# honest instead, exactly as it does for ``local_to_jira_status`` / ``jira_to_local_status``.
LOCAL_TYPE_TO_JIRA: dict[str, str] = {
    "bug": "Bug",
    "story": "Story",
    "task": "Task",
    "epic": "Epic",
}

# Canonical reverse TYPE mapping: Jira issue type name -> local ticket type. Mirrors
# ``jira_to_local_status`` above: a SECOND, INDEPENDENT literal (deliberately not an
# import) of ``inbound_fields._JIRA_TO_LOCAL_TYPE``, kept in lock-step with it by a
# parity test. ``outbound_labels`` imports ONLY this module, so the ``rebar-type:``
# stamp rule (``_desired_type_annotation``) reads the Jira->local reverse from here
# rather than reaching into an adapter package (which would invert the one-way
# core<-adapter layering, exactly as the comment on ``jira_to_local_status`` explains).
# The built-in type map is bijective, so this is its exact inverse today; a per-project
# ``type_map`` overlay may collapse two local types onto one Jira type, and THAT lossy
# case is what the annotation label recovers.
jira_to_local_type: dict[str, str] = {
    "Bug": "bug",
    "Story": "story",
    "Task": "task",
    "Epic": "epic",
}


def effective_type_map(project_key: str, root: object = None) -> dict[str, str]:
    """Resolve the EFFECTIVE outbound local->Jira type map for ``project_key``.

    The forward map is the built-in :data:`LOCAL_TYPE_TO_JIRA` (this module's own literal)
    <- ``[mapping.default.type_map]`` <- ``[mapping.projects.<KEY>.type_map]``, resolved
    through the S1 per-key three-layer merge (``mapping_config.resolve_for_project``). A
    ``SKIP`` value (``mapping_config.SKIP``) means the local type has NO Jira target for
    this project (type-granular skip) — dropped here so callers see only mappable types
    (its exclusion is surfaced separately via :func:`effective_excluded_sync_types`).

    With NO ``[mapping]`` block the result equals :data:`LOCAL_TYPE_TO_JIRA` verbatim —
    the config seam is inert until configured.

    The built-in map is read as a MODULE GLOBAL at call time (never captured at import),
    so a test may simulate an undecided type by monkeypatching this module's
    ``LOCAL_TYPE_TO_JIRA``. ``mapping_config`` is imported LAZILY so this module stays
    stdlib-only at import time (it imports nothing but ``__future__`` at the top level)."""
    from rebar_reconciler import mapping_config as mc

    cfg = mc.load_mapping_config(root)
    builtin = mc.MappingLayer(type_map=LOCAL_TYPE_TO_JIRA)
    resolved = mc.resolve_for_project(cfg, project_key, builtin=builtin)
    return {k: v for k, v in resolved.type_map.items() if v != mc.SKIP}


def effective_excluded_sync_types(project_key: str, root: object = None) -> set[str]:
    """The EFFECTIVE set of local ticket types NOT synced to Jira for ``project_key``.

    The built-in :data:`EXCLUDED_SYNC_TYPES` (session_log/code_review/identity) UNION
    every local type mapped to ``mapping_config.SKIP`` in the SAME resolved layer
    :func:`effective_type_map` uses. Reuses S1's ``SKIP`` sentinel as the type-granular
    skip signal (no separate axis): a local type mapped to ``SKIP`` is excluded for that
    project ON TOP of the built-in set."""
    from rebar_reconciler import mapping_config as mc

    cfg = mc.load_mapping_config(root)
    builtin = mc.MappingLayer(type_map=LOCAL_TYPE_TO_JIRA)
    resolved = mc.resolve_for_project(cfg, project_key, builtin=builtin)
    return set(EXCLUDED_SYNC_TYPES) | {k for k, v in resolved.type_map.items() if v == mc.SKIP}


def assert_type_decisions_complete(project_key: str, root: object = None) -> None:
    """Fail-closed gate: every SYNCABLE local ticket type must be DECIDED for a project.

    A syncable type is a member of ``rebar.types.TicketType`` MINUS the built-in
    :data:`EXCLUDED_SYNC_TYPES`. Each such type must be DECIDED — present in
    :func:`effective_type_map` (a Jira target) OR in :func:`effective_excluded_sync_types`
    (mapped to ``SKIP``). An undecided type would be silently coerced to a default Jira
    type downstream; this gate instead raises :class:`mapping_config.MappingConfigError`
    naming the undecided type(s) so an operator's incomplete ``[mapping.*.type_map]``
    (or a newly added ``TicketType`` with no sync decision) fails loudly, up front.

    The built-in :data:`LOCAL_TYPE_TO_JIRA` covers all four syncable types today, so with
    no ``[mapping]`` block this never fires. The syncable vocabulary and the built-in map
    are both read at call time (the map via :func:`effective_type_map`, which reads this
    module's global), so a test may simulate an undecided type by monkeypatching the
    built-in."""
    from typing import get_args

    from rebar.types import TicketType
    from rebar_reconciler import mapping_config as mc

    syncable = set(get_args(TicketType)) - set(EXCLUDED_SYNC_TYPES)
    decided = set(effective_type_map(project_key, root)) | effective_excluded_sync_types(
        project_key, root
    )
    undecided = sorted(syncable - decided)
    if undecided:
        raise mc.MappingConfigError(
            "type mapping is incomplete for project "
            f"{project_key!r}: no decision (a Jira target or {mc.SKIP!r}) for syncable "
            f"ticket type(s): {', '.join(undecided)}"
        )


# Local relation -> Jira issue-link TYPE name (S4). Like ``local_to_jira_status`` and
# ``LOCAL_TYPE_TO_JIRA`` above, this is a SECOND, INDEPENDENT literal of the same mapping
# (the SOLE definition site under ``adapters/`` is the link-TYPE component of
# ``adapters/jira_family/value_maps.RELATION_TO_JIRA_LINK``) and it is deliberately NOT an
# import: this module imports nothing but ``__future__``, and ``adapters/jira_family`` is a
# VENDOR package whose dependency direction is one-way (concrete backends import it; it never
# imports core back), so importing it here would invert that layering and put this
# operator-overridable surface behind an adapter import — the import-graph contract test
# enforces exactly this. This literal carries the link TYPE only (not the direction ``swap``,
# which stays with the built-in adapter payload); the parity TEST keeps the two in lock-step,
# exactly as it does for ``local_to_jira_status`` / ``LOCAL_TYPE_TO_JIRA``.
local_to_jira_link: dict[str, str] = {
    "blocks": "Blocks",
    "depends_on": "Blocks",
    "relates_to": "Relates",
}


def effective_link_map(project_key: str, root: object = None) -> dict[str, str]:
    """Resolve the EFFECTIVE outbound relation->Jira link-type map for ``project_key``.

    The forward map is the built-in :data:`local_to_jira_link` (this module's own literal)
    <- ``[mapping.default.link_map]`` <- ``[mapping.projects.<KEY>.link_map]``, resolved
    through the S1 per-key three-layer merge (``mapping_config.resolve_for_project``). A
    ``SKIP`` value (``mapping_config.SKIP``) means the relation has NO Jira target for this
    project (relation-granular skip) — dropped here so callers see only mappable relations.

    Fail-closed: ``mapping_config.validate`` raises :class:`mapping_config.MappingConfigError`
    when a ``link_map`` value falls outside a declared ``link_types`` vocabulary (``SKIP`` is
    always allowed) — a relation is mapped to a real declared link type, skipped, or the pass
    fails; never approximated.

    With NO ``[mapping]`` block the result equals :data:`local_to_jira_link` verbatim — the
    config seam is inert until configured. ``mapping_config`` is imported LAZILY so this
    module stays stdlib-only at import time (it imports nothing but ``__future__`` at the top
    level — the operator-overridable link literal above must never sit behind an adapter/config
    import)."""
    from rebar_reconciler import mapping_config as mc

    cfg = mc.load_mapping_config(root)
    builtin = mc.MappingLayer(link_map=local_to_jira_link)
    resolved = mc.resolve_for_project(cfg, project_key, builtin=builtin)
    mc.validate(resolved, mc.Capability(has_link_types=True))
    return {k: v for k, v in resolved.link_map.items() if v != mc.SKIP}


# Local priority integer -> Jira priority NAME (S5). Like ``local_to_jira_status``,
# ``LOCAL_TYPE_TO_JIRA`` and ``local_to_jira_link`` above, this is a SECOND, INDEPENDENT
# literal of the same mapping (the SOLE definition site under ``adapters/`` is
# ``adapters/jira_family/value_maps.LOCAL_PRIORITY_TO_JIRA``) and it is deliberately NOT an
# import: this module imports nothing but ``__future__``, and ``adapters/jira_family`` is a
# VENDOR package whose dependency direction is one-way (concrete backends import it; it never
# imports core back), so importing it here would invert that layering and put this
# operator-overridable surface behind an adapter import — the import-graph contract test
# enforces exactly this. NOTE the two literals have DIFFERENT key TYPES on purpose: the
# adapter-side ``LOCAL_PRIORITY_TO_JIRA`` is INT-keyed (``dict[int, str]``, 0-4), while the
# config seam's keys are STRINGS (a ``[mapping.*.priority_map]`` TOML sub-table has string
# keys), so this literal is str-keyed. A parity TEST keeps the two honest (the same
# priority -> name pairs), exactly as it does for the other three axes.
local_to_jira_priority: dict[str, str] = {
    "0": "Highest",
    "1": "High",
    "2": "Medium",
    "3": "Low",
    "4": "Lowest",
}


def effective_priority_map(project_key: str, root: object = None) -> dict[str, str]:
    """Resolve the EFFECTIVE outbound local->Jira priority map for ``project_key``.

    The forward map is the built-in :data:`local_to_jira_priority` (this module's own
    str-keyed literal) <- ``[mapping.default.priority_map]`` <-
    ``[mapping.projects.<KEY>.priority_map]``, resolved through the S1 per-key three-layer
    merge (``mapping_config.resolve_for_project``). A ``SKIP`` value
    (``mapping_config.SKIP``) or an absent key means the local priority has NO Jira target
    — dropped here so callers see only mappable priorities (map-or-drift: a caller that
    gets no target for a local priority OMITS the field rather than coercing it).

    Priority is a SOFT axis: it has NO vocabulary declaration, so there is NO
    ``mapping_config.validate`` call (unlike :func:`effective_link_map`, which fail-closes
    on an out-of-vocabulary link type). An unmapped priority simply DRIFTS.

    With NO ``[mapping]`` block the result equals :data:`local_to_jira_priority` verbatim —
    the config seam is inert until configured. ``mapping_config`` is imported LAZILY so this
    module stays stdlib-only at import time (it imports nothing but ``__future__`` at the top
    level — the operator-overridable priority literal above must never sit behind an
    adapter/config import)."""
    from rebar_reconciler import mapping_config as mc

    cfg = mc.load_mapping_config(root)
    builtin = mc.MappingLayer(priority_map=local_to_jira_priority)
    resolved = mc.resolve_for_project(cfg, project_key, builtin=builtin)
    return {k: v for k, v in resolved.priority_map.items() if v != mc.SKIP}


def effective_create_defaults(project_key: str, root: object = None) -> dict[str, str]:
    """Resolve the EFFECTIVE per-project str-valued ``create_defaults`` for ``project_key``.

    ``create_defaults`` is a per-project axis of vendor field name -> literal string value,
    merged into the CREATE body for required-beyond-baseline Jira fields (baseline computed
    fields win on collision at the mapper). There is NO built-in defaults literal — an empty
    ``MappingLayer`` is the built-in — so with NO ``[mapping]`` block the result is ``{}``,
    the config seam inert until configured. Resolved through the same S1 per-key three-layer
    merge (``[mapping.default.create_defaults]`` <- ``[mapping.projects.<KEY>.create_defaults]``);
    any ``SKIP`` value drops the field. This axis is ungated (no vocabulary), and it is
    CREATE-only — UPDATE applies none.

    ``mapping_config`` is imported LAZILY so this module stays stdlib-only at import time."""
    from rebar_reconciler import mapping_config as mc

    cfg = mc.load_mapping_config(root)
    builtin = mc.MappingLayer(create_defaults={})
    resolved = mc.resolve_for_project(cfg, project_key, builtin=builtin)
    return {k: v for k, v in resolved.create_defaults.items() if v != mc.SKIP}
