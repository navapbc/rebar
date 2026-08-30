"""Per-project effective-mapping resolvers for the outbound differ.

Split out of ``outbound_differ.py`` for module-size headroom (ticket
7153-e5ad-5e20-4ae9, ahead of the typed-payload cutover). This cluster is
self-contained: ``_memoize_effective`` is the pass-scoped memo helper, and the
six ``_effective_*_for`` functions are its only callers — each resolves one
axis's per-project overlay (status / type / priority / create-defaults / link
/ excluded-sync-types) via ``projects_store.resolve_project`` +
``config.effective_*``, threading the memo cache through so expensive
per-project config discovery runs once per axis per pass rather than once per
ticket.

Re-exported from ``outbound_differ.py`` (mirroring the ``outbound_assignee`` /
``outbound_comments`` / ``outbound_labels`` / ``outbound_links`` splits already
in that file) so ``outbound_differ.<name>`` keeps resolving for existing
callers (``outbound_mutation_builders.py``) and tests.
"""

from __future__ import annotations

from typing import Any


def _memoize_effective(
    cache: dict[tuple[str, str], Any] | None,
    axis: str,
    project_key: str,
    compute: Any,
) -> Any:
    """Pass-scoped memo for a per-project effective map (d378).

    ``cache`` is a dict local to one ``compute_outbound_mutations`` pass, keyed by
    ``(axis, project_key)``. On a miss it runs ``compute`` — the per-project config
    discovery (``_discover_project_config`` filesystem stat-walk) plus the three-layer
    overlay resolution — and stores the result, so that expensive work runs ONCE per
    distinct project per axis per pass rather than once per ticket. Keying on both
    ``axis`` and ``project_key`` keeps every project's map distinct (no cross-project
    leak). ``cache is None`` disables memoization and computes on every call, so every
    direct caller/test of the ``_effective_*_for`` helpers is byte-for-byte unchanged."""
    if cache is None:
        return compute()
    key = (axis, project_key)
    if key not in cache:
        cache[key] = compute()
    return cache[key]


def _effective_status_map_for(
    ticket: dict[str, Any], mapping: Any, repo_root: Any = None, *, cache: Any = None
) -> dict[str, str] | None:
    """The effective per-project local->Jira status map for ``ticket``, or ``None``.

    Story S2: resolve the ticket's project (``projects_store.resolve_project``) and return
    ``config.effective_status_map(project_key, root=repo_root)`` so both create and update
    map status through the project's overlay (map-or-drift). ``repo_root`` is the store root
    the pass runs against; ``[mapping]`` MUST be read from it, never the CWD, consistent with
    the fetcher and preflight. ``None`` (the built-in fallback the mappers already apply) when
    no project key is obtainable or there is no ``[mapping]`` block."""
    if mapping is None or not getattr(mapping, "projects", None):
        return None
    from rebar_reconciler import config, projects_store

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
    """The effective per-project local->Jira TYPE map for ``ticket``, or ``None``.

    Story S3: the type-axis mirror of ``_effective_status_map_for``. Resolve the ticket's
    project (``projects_store.resolve_project``) and return
    ``config.effective_type_map(project_key, root=repo_root)`` so both create and update
    map ``issuetype`` through the project's overlay. ``repo_root`` is the store root the
    pass runs against; ``[mapping]`` MUST be read from it, never the CWD. ``None`` (the
    built-in fallback the mappers already apply) when no project key is obtainable or
    there is no ``[mapping]`` block."""
    if mapping is None or not getattr(mapping, "projects", None):
        return None
    from rebar_reconciler import config, projects_store

    project_key = projects_store.resolve_project(ticket, mapping)
    if not project_key:
        return None
    return _memoize_effective(
        cache, "type", project_key, lambda: config.effective_type_map(project_key, root=repo_root)
    )


def _effective_priority_map_for(
    ticket: dict[str, Any], mapping: Any, repo_root: Any = None, *, cache: Any = None
) -> dict[str, str] | None:
    """The effective per-project local->Jira PRIORITY map for ``ticket``, or ``None``.

    Story S5: the priority-axis mirror of ``_effective_status_map_for``. Resolve the
    ticket's project (``projects_store.resolve_project``) and return
    ``config.effective_priority_map(project_key, root=repo_root)`` so both create and
    update map ``priority`` through the project's overlay (map-or-drift). ``repo_root`` is
    the store root the pass runs against; ``[mapping]`` MUST be read from it, never the
    CWD. ``None`` (the built-in fallback the mappers already apply) when no project key is
    obtainable or there is no ``[mapping]`` block."""
    if mapping is None or not getattr(mapping, "projects", None):
        return None
    from rebar_reconciler import config, projects_store

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
    """The effective per-project str-valued ``create_defaults`` for ``ticket``, or ``None``.

    Story S5: the CREATE-only axis of required-beyond-baseline vendor fields. Resolve the
    ticket's project (``projects_store.resolve_project``) and return
    ``config.effective_create_defaults(project_key, root=repo_root)``. ``repo_root`` is the
    store root the pass runs against; ``[mapping]`` MUST be read from it, never the CWD.
    ``None`` (no defaults merged) when no project key is obtainable or there is no
    ``[mapping]`` block."""
    if mapping is None or not getattr(mapping, "projects", None):
        return None
    from rebar_reconciler import config, projects_store

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
    """The effective per-project relation->Jira link-type map for ``ticket``, or ``None``.

    Story S4: the link-axis mirror of ``_effective_status_map_for``. Resolve the ticket's
    project (``projects_store.resolve_project``) and return the resolved ``link_map`` so the
    UPDATE link diff maps relations through the project's overlay. ``repo_root`` is the store
    root the pass runs against; ``[mapping]`` MUST be read from it, never the CWD. ``None``
    (the built-in fallback the diff funcs already apply) when no project key is obtainable or
    there is no ``[mapping]`` block.

    Unlike ``config.effective_link_map`` (which DROPS ``SKIP`` for its callers), this RETAINS
    ``SKIP`` entries: the diff funcs must distinguish a forced skip (suppress ADD and REMOVE)
    from an absent key (fall through to the built-in payload). Fail-closed validation
    (``mapping_config.validate``) still fires on an out-of-vocabulary link type."""
    if mapping is None or not getattr(mapping, "projects", None):
        return None
    from rebar_reconciler import config, projects_store
    from rebar_reconciler import mapping_config as mc

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
    """The effective per-project excluded-sync-type set for ``ticket``.

    Story S3: a local type mapped to ``mapping_config.SKIP`` is excluded for that project
    ON TOP of the built-in :data:`config.EXCLUDED_SYNC_TYPES`. Resolve the ticket's project
    and return ``config.effective_excluded_sync_types(project_key, root=repo_root)``; fall
    back to ``builtin`` (the pass-level base set) when no mapping/project applies, so the
    no-config path is unchanged."""
    if mapping is None or not getattr(mapping, "projects", None):
        return builtin
    from rebar_reconciler import config, projects_store

    project_key = projects_store.resolve_project(ticket, mapping)
    if not project_key:
        return builtin
    return _memoize_effective(
        cache,
        "excluded_sync_types",
        project_key,
        lambda: config.effective_excluded_sync_types(project_key, root=repo_root),
    )
