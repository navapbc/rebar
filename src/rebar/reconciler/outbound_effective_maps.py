"""Resolve outbound mapping overlays per project and axis.

The pass cache avoids repeated project config discovery. Config comes from
``repo_root``, never the process directory. ``outbound_differ`` re-exports
these helpers for existing callers and tests.
"""

from __future__ import annotations

from typing import Any


def _memoize_effective(
    cache: dict[tuple[str, str], Any] | None,
    axis: str,
    project_key: str,
    compute: Any,
) -> Any:
    """Cache one effective map per axis and project for this pass.

    ``None`` disables caching for direct callers. The composite key prevents
    one project's map from leaking into another.
    """
    if cache is None:
        return compute()
    key = (axis, project_key)
    if key not in cache:
        cache[key] = compute()
    return cache[key]


def _effective_status_map_for(
    ticket: dict[str, Any], mapping: Any, repo_root: Any = None, *, cache: Any = None
) -> dict[str, str] | None:
    """Resolve the project's status map from ``repo_root``, or use the built-in fallback."""
    if mapping is None or not getattr(mapping, "projects", None):
        return None
    from rebar.reconciler import config, projects_store

    project_key = projects_store.resolve_project(ticket, mapping)
    if not project_key:
        return None
    return _memoize_effective(
        cache,
        "status",
        project_key,
        lambda: config.effective_status_map(project_key, root=repo_root),
    )


def _effective_type_map_for(
    ticket: dict[str, Any], mapping: Any, repo_root: Any = None, *, cache: Any = None
) -> dict[str, str] | None:
    """Resolve the project's issue-type map from ``repo_root``, or use the fallback."""
    if mapping is None or not getattr(mapping, "projects", None):
        return None
    from rebar.reconciler import config, projects_store

    project_key = projects_store.resolve_project(ticket, mapping)
    if not project_key:
        return None
    return _memoize_effective(
        cache, "type", project_key, lambda: config.effective_type_map(project_key, root=repo_root)
    )


def _effective_priority_map_for(
    ticket: dict[str, Any], mapping: Any, repo_root: Any = None, *, cache: Any = None
) -> dict[str, str] | None:
    """Resolve the project's priority map from ``repo_root``, or use the fallback."""
    if mapping is None or not getattr(mapping, "projects", None):
        return None
    from rebar.reconciler import config, projects_store

    project_key = projects_store.resolve_project(ticket, mapping)
    if not project_key:
        return None
    return _memoize_effective(
        cache,
        "priority",
        project_key,
        lambda: config.effective_priority_map(project_key, root=repo_root),
    )


def _effective_create_defaults_for(
    ticket: dict[str, Any], mapping: Any, repo_root: Any = None, *, cache: Any = None
) -> dict[str, str] | None:
    """Resolve the project's create-only vendor defaults, or return ``None``."""
    if mapping is None or not getattr(mapping, "projects", None):
        return None
    from rebar.reconciler import config, projects_store

    project_key = projects_store.resolve_project(ticket, mapping)
    if not project_key:
        return None
    return _memoize_effective(
        cache,
        "create_defaults",
        project_key,
        lambda: config.effective_create_defaults(project_key, root=repo_root),
    )


def _effective_link_map_for(
    ticket: dict[str, Any], mapping: Any, repo_root: Any = None, *, cache: Any = None
) -> dict[str, str] | None:
    """Resolve the project's link map, retaining ``SKIP`` entries.

    The link differ needs ``SKIP`` to suppress both add and remove; an absent
    key falls through to the built-in map. Invalid link types still fail validation.
    """
    if mapping is None or not getattr(mapping, "projects", None):
        return None
    from rebar.reconciler import config, projects_store
    from rebar.reconciler import mapping_config as mc

    project_key = projects_store.resolve_project(ticket, mapping)
    if not project_key:
        return None

    def _compute() -> dict[str, str]:
        cfg = mc.load_mapping_config(repo_root)
        builtin = mc.MappingLayer(link_map=config.local_to_jira_link)
        resolved = mc.resolve_for_project(cfg, project_key, builtin=builtin)
        mc.validate(resolved, mc.Capability(has_link_types=True))
        return dict(resolved.link_map)

    return _memoize_effective(cache, "link", project_key, _compute)


def _effective_excluded_sync_types_for(
    ticket: dict[str, Any], mapping: Any, repo_root: Any, *, builtin: Any, cache: Any = None
) -> Any:
    """Return project exclusions, including mapped ``SKIP`` types, or ``builtin``."""
    if mapping is None or not getattr(mapping, "projects", None):
        return builtin
    from rebar.reconciler import config, projects_store

    project_key = projects_store.resolve_project(ticket, mapping)
    if not project_key:
        return builtin
    return _memoize_effective(
        cache,
        "excluded_sync_types",
        project_key,
        lambda: config.effective_excluded_sync_types(project_key, root=repo_root),
    )
