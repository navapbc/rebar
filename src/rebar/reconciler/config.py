"""Configuration constants for rebar.reconciler."""

from __future__ import annotations

import os
from typing import Any

EXCLUDED_FIELDS: tuple[str, ...] = ("local_id", "rebar-id")

# --------------------------------------------------------------------------- #
# JQL visibility-poll backoff knobs (owned env-read seam).
# --------------------------------------------------------------------------- #
# The STEP_JQL_SEARCH visibility wait in access_check is a capped-exponential backoff
# tunable by three operator env knobs. Those env reads live HERE — the reconciler's
# config composition root, which owns the env-read category — so access_check (a
# credential boundary that does NOT own generic knob reads) stays free of ambient
# knob reads and needs no ``# read-via:`` suppression marker.

JQL_RETRY_COUNT = 10
JQL_RETRY_SLEEP = 3
JQL_RETRY_SLEEP_MAX = 30


def _int_from_env(src: dict[str, str] | Any, key: str, default: int) -> int:
    """Parse a positive int env override; fall back to ``default`` when unset/invalid."""
    raw = src.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _float_from_env(src: dict[str, str] | Any, key: str, default: float) -> float:
    """Parse a positive float env override; fall back to ``default`` when unset/invalid."""
    raw = src.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(str(raw).strip())
    except ValueError:
        return default
    return value if value > 0 else default


def resolve_jql_backoff(env: dict[str, str] | None = None) -> tuple[int, float, float]:
    """Resolve the JQL visibility-poll backoff from env, at CALL time (defaults otherwise).

    ``JIRA_PROBE_JQL_RETRIES`` (attempts), ``JIRA_PROBE_JQL_SLEEP`` (base seconds), and
    ``JIRA_PROBE_JQL_SLEEP_MAX`` (cap seconds) let an operator widen the wait for a slower
    index without a code change; a missing / non-numeric / non-positive value falls back
    to the module default.
    """
    src = os.environ if env is None else env
    retries = _int_from_env(src, "JIRA_PROBE_JQL_RETRIES", JQL_RETRY_COUNT)
    base = _float_from_env(src, "JIRA_PROBE_JQL_SLEEP", JQL_RETRY_SLEEP)
    cap = _float_from_env(src, "JIRA_PROBE_JQL_SLEEP_MAX", JQL_RETRY_SLEEP_MAX)
    return retries, base, cap


# These local artifact types never sync to Jira. The outbound type map omits them
# so a missed exclusion fails visibly.
EXCLUDED_SYNC_TYPES: frozenset[str] = frozenset({"session_log", "code_review", "identity"})

# Map local statuses to Jira workflow names. Preflight rejects an update whose
# status is absent here. An empty map is valid when no status update is planned.
# Keep this independent core literal equal to jira_family.value_maps through the
# parity test, without importing the adapter into core.
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

# The reverse status map is canonical because the forward map is not injective.
# Annotation labels restore blocked or cancelled distinctions. Keep this literal
# in parity with inbound_fields rather than inverting local_to_jira_status.
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
    from rebar.reconciler import mapping_config as mc

    cfg = mc.load_mapping_config(root)
    builtin = mc.MappingLayer(status_map=local_to_jira_status)
    resolved = mc.resolve_for_project(cfg, project_key, builtin=builtin)
    return {k: v for k, v in resolved.status_map.items() if v != mc.SKIP}


# This operator-overridable core type map mirrors jira_family.value_maps through
# a parity test. Importing the adapter here would invert the dependency direction.
LOCAL_TYPE_TO_JIRA: dict[str, str] = {
    "bug": "Bug",
    "story": "Story",
    "task": "Task",
    "epic": "Epic",
}

# The canonical reverse type map stays in core for outbound rebar-type labels.
# Parity with inbound_fields protects the independent literal. Project mappings
# may collapse local types, and the label recovers that lost distinction.
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
    from rebar.reconciler import mapping_config as mc

    cfg = mc.load_mapping_config(root)
    builtin = mc.MappingLayer(type_map=LOCAL_TYPE_TO_JIRA)
    resolved = mc.resolve_for_project(cfg, project_key, builtin=builtin)
    return {k: v for k, v in resolved.type_map.items() if v != mc.SKIP}


def effective_excluded_sync_types(project_key: str, root: object = None) -> set[str]:
    """Return built-in excluded ticket types plus project types mapped to SKIP in the
    same resolved layer used by effective_type_map."""
    from rebar.reconciler import mapping_config as mc

    cfg = mc.load_mapping_config(root)
    builtin = mc.MappingLayer(type_map=LOCAL_TYPE_TO_JIRA)
    resolved = mc.resolve_for_project(cfg, project_key, builtin=builtin)
    return set(EXCLUDED_SYNC_TYPES) | {k for k, v in resolved.type_map.items() if v == mc.SKIP}


def assert_type_decisions_complete(project_key: str, root: object = None) -> None:
    """Require every syncable TicketType to map to Jira or SKIP for this project.
    Raise MappingConfigError with undecided types before a default issue type could
    silently take their place. Read the type vocabulary and built-in mapping at call
    time so current configuration governs the decision."""
    from typing import get_args

    from rebar.reconciler import mapping_config as mc
    from rebar.types import TicketType

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


# This core relation map keeps only Jira link type names. Link direction remains
# in the adapter payload. A parity test keeps this independent literal aligned
# without importing the vendor adapter into core.
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
    from rebar.reconciler import mapping_config as mc

    cfg = mc.load_mapping_config(root)
    builtin = mc.MappingLayer(link_map=local_to_jira_link)
    resolved = mc.resolve_for_project(cfg, project_key, builtin=builtin)
    mc.validate(resolved, mc.Capability(has_link_types=True))
    return {k: v for k, v in resolved.link_map.items() if v != mc.SKIP}


# The core priority map has string keys for TOML project overrides. The adapter
# literal has integer keys. A parity test checks their priority-to-name pairs
# without importing the vendor adapter into core.
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
    from rebar.reconciler import mapping_config as mc

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
    from rebar.reconciler import mapping_config as mc

    cfg = mc.load_mapping_config(root)
    builtin = mc.MappingLayer(create_defaults={})
    resolved = mc.resolve_for_project(cfg, project_key, builtin=builtin)
    return {k: v for k, v in resolved.create_defaults.items() if v != mc.SKIP}
