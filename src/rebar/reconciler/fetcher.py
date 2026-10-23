#!/usr/bin/env python3
"""Fetcher: pull a normalized Jira snapshot and write it to .rebar/bridge/snapshots/.

fetch_snapshot(pass_id) calls AcliClient.search_issues() with the filtered JQL,
paginates through the working set via ``_iter_pages``, dedups cross-page
duplicates while emitting an observable alert, enforces the 1000-issue ACLI
ceiling by raising ``SilentTruncationError``, and writes the normalized snapshot
as sorted-key JSON to .rebar/bridge/snapshots/<pass_id>.json.

Two fetches over identical remote data produce byte-identical files (idempotent).
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
import sys
import time
import urllib.error
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Port type for `client` params (gate cc77): imported RELATIVELY so mypy can
    # resolve it (a `rebar.reconciler.`-qualified import widens to Any under
    # ignore_missing_imports). TYPE_CHECKING-only, so the standalone
    # importlib-by-path load — where a relative runtime import has no package —
    # is unaffected.
    from ._backend import TicketTransport

# Absolute imports support standalone by-path loading.
from rebar.config import reconciler_repo_root
from rebar.reconciler._backend import BackendPaginationStallError
from rebar.reconciler.fetch_paging import (  # noqa: F401
    _ACLI_CEILING,
    SilentTruncationError,
    _extract_issues,
    _iter_pages,
    collect,
    drain,
    enrich_local_id_properties,
)
from rebar.reconciler.scratch_retention import snapshots_dir

# Split active and recent-Done queries under the configured project key.
# Cap Done at 1000 by updated time. Older Done issues remain outside the fetch
# window. Validate the project key before constructing either scoped JQL query.
_PROJECT_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _validate_project_key(project: str) -> str:
    """Return ``project`` if it is a syntactically valid Jira project key, else raise.

    Guards both correctness (an empty key would make ACLI search every project) and
    JQL-injection safety (the key is interpolated unquoted into the JQL).
    """
    if not project or not _PROJECT_KEY_RE.match(project):
        raise ValueError(
            f"inbound fetch requires a valid jira.project key "
            f"(JIRA_PROJECT / [jira] project); got {project!r}. "
            f"Refusing to search Jira unscoped."
        )
    return project


def jql_active(project: str) -> str:
    """Active-working-set query (``statusCategory != \"Done\"``) scoped to ``project``."""
    return f'project = {_validate_project_key(project)} AND statusCategory != "Done"'


def jql_done_recent(project: str) -> str:
    """Recent-Done query (``ORDER BY updated DESC``) scoped to ``project``."""
    key = _validate_project_key(project)
    return f'project = {key} AND statusCategory = "Done" ORDER BY updated DESC'


def jqls_for(project: str) -> tuple[str, str]:
    """The ordered (active, done-recent) JQL pair for ``project``."""
    return (jql_active(project), jql_done_recent(project))


# The Done query sorts by update time before this client-side cap.
_DONE_RECENT_CAP = 1000


def _load_acli():
    """Lazily select the configured backend transport to avoid import cycles and retain
    standalone by-path loading."""
    from rebar.config import compose_config
    from rebar.reconciler._backend_registry import select_backend

    return select_backend(compose_config()).transport


# Register alert_store under its canonical dotted key so production loads and
# test patches share one module object.
_ALERT_STORE_KEY = "rebar.reconciler.alert_store"


def _load_alert_store():
    """Lazy-load alert_store under its canonical sys.modules key.

    Production callers (fetcher.fetch_snapshot dedup-alert path) need
    alert_store at runtime. This helper performs an importlib-based sibling
    load and registers it under the canonical ``rebar.reconciler.alert_store``
    dotted key so any other loader / test patch sees the same module object.

    On exec_module failure, the partially-initialised module is removed
    from sys.modules before re-raising so a subsequent call retries
    cleanly rather than reusing a broken module (copilot review finding
    on PR #363).
    """
    if _ALERT_STORE_KEY in sys.modules:
        return sys.modules[_ALERT_STORE_KEY]
    mod = importlib.import_module("rebar.reconciler.alert_store")
    return mod


def _known_jira_statuses(repo_root: Path) -> frozenset[str]:
    """Jira workflow status NAMES the reconciler can round-trip: the built-in
    reverse-map keys (``config.jira_to_local_status``) UNION every Jira status NAME the
    mapping config for ``repo_root`` declares (``mapping_config.declared_status_names``).
    An empty BUILT-IN mapping disables the check (the preflight kill-switch)."""
    from rebar.reconciler import config as _cfg
    from rebar.reconciler import mapping_config as _mc

    builtin = getattr(_cfg, "jira_to_local_status", {}) or {}
    if not builtin:
        return frozenset()  # kill-switch: empty built-in mapping disables the check
    declared = _mc.declared_status_names(_mc.load_mapping_config(repo_root))
    return frozenset(set(builtin) | declared)


def _flag_unmapped_statuses(
    snapshot: dict[str, dict],
    pass_id: str,
    repo_root: Path,
    alert_store: Any,
    log: Any,
) -> None:
    """Warn + emit an observable bridge_alert for any Jira workflow status in
    ``snapshot`` that the reconciler has no mapping for (see
    :func:`_known_jira_statuses`).

    Fires at most once per DISTINCT unmapped status per pass (a local ``seen`` set),
    and de-duplicates the observable alert across passes via ``alert_store.is_deduped``
    (24h window) so a persistent unmapped status does not re-file every ~20-minute
    pass. Fully fail-open: any error is logged and swallowed — proactive detection
    must never break the fetch/reconcile pass."""
    known = _known_jira_statuses(repo_root)
    if not known:
        return  # kill-switch: an empty mapping disables the check
    seen: set[str] = set()
    for snap_key, snap_fields in snapshot.items():
        status_obj = snap_fields.get("status") if isinstance(snap_fields, dict) else None
        name = status_obj.get("name") if isinstance(status_obj, dict) else status_obj
        if not isinstance(name, str) or not name or name in known or name in seen:
            continue
        seen.add(name)
        log.warning(
            "fetch_snapshot: Jira status %r (e.g. %s) has no reconciler mapping in "
            "config.jira_to_local_status — add a mapping, or it will trip the outbound "
            "status preflight if it reaches a mutation. (pass %s)",
            name,
            snap_key,
            pass_id,
        )
        dedup_key = f"unmapped-jira-status:{name}"
        try:
            if not alert_store.is_deduped(dedup_key, repo_root=repo_root):
                alert_store.append(
                    {
                        "kind": "fetcher-unmapped-jira-status",
                        "key": dedup_key,
                        "status": name,
                        "example_issue": snap_key,
                        "pass_id": pass_id,
                        "timestamp_ns": time.time_ns(),
                    },
                    repo_root=repo_root,
                )
        except Exception as exc:  # noqa: BLE001 — observability write is best-effort; never fail the fetch
            log.warning(
                "fetch_snapshot: failed to emit unmapped-status alert for %r (%r)",
                name,
                exc,
            )


def merge_parent_map(
    snapshot: dict[str, dict],
    parent_map: dict[str, str | None],
) -> dict[str, dict]:
    """Merge observed parents while preserving 3 states. A missing map key means the
    issue was not observed and leaves parent absent. A nonempty value writes its
    parent key. An explicit None records an authoritative parent clear. The inbound
    differ must never infer a destructive clear from missing enrichment."""
    for snap_key, parent_jira_key in parent_map.items():
        if snap_key not in snapshot:
            continue
        snapshot[snap_key]["parent"] = {"key": parent_jira_key} if parent_jira_key else None
    return snapshot


def merge_issuelinks_map(
    snapshot: dict[str, dict],
    issuelinks_map: dict[str, Any],
) -> dict[str, dict]:
    """Merge observed Jira links by key presence. A missing key leaves issuelinks
    unobserved, while an explicit empty list proves no links and may support a
    managed removal. Failed or skipped enrichment cannot imply a removal."""
    for snap_key, links in issuelinks_map.items():
        if snap_key in snapshot and isinstance(links, list):
            snapshot[snap_key]["issuelinks"] = links
    return snapshot


def _enrich_parents(client: TicketTransport, project_key: str, snapshot: dict, log) -> None:
    """Parent enrichment for ONE project (story 1734 fan-out helper). Fail-open;
    BackendPaginationStallError re-raises out. Mutates ``snapshot`` in place."""
    try:
        if project_key and hasattr(client, "get_parent_map"):
            # merge_parent_map (ticket 88d9) owns the three-state
            # queried/absent/unobserved contract so it is directly testable.
            merge_parent_map(snapshot, client.get_parent_map(project_key))
    except urllib.error.HTTPError as exc:
        # Report retired API routes loudly. Other HTTP faults remain warnings.
        if exc.code == 410:
            log.error(
                "fetch_snapshot: parent enrichment hit HTTP 410 GONE — the Jira "
                "search endpoint has been RETIRED; snapshot written without parent "
                "data (degraded). API retirement, not a transient fault: %r",
                exc,
            )
        else:
            log.warning(
                "fetch_snapshot: parent enrichment failed (HTTP %s: %r); "
                "snapshot written without parent data (degraded)",
                exc.code,
                exc,
            )
    except BackendPaginationStallError:
        # A stalled whole-project parent read must not masquerade as an authoritative
        # empty map.
        raise
    except Exception as exc:  # noqa: BLE001 — fail-open: skip parent enrichment, write degraded snapshot
        log.warning(
            "fetch_snapshot: parent enrichment failed (%r); "
            "snapshot written without parent data (degraded)",
            exc,
        )


def _enrich_comments(client: TicketTransport, project_key: str, snapshot: dict, log) -> None:
    """Enrich one project in place. Pager stalls raise. Other faults defer."""
    # Use one paged comment-map read per project. Only returned comment fields
    # become observed. Others retain the per-ticket fallback.
    try:
        if project_key and hasattr(client, "get_comment_map"):
            comment_map = client.get_comment_map(project_key)
            for snap_key, comment_field in comment_map.items():
                if snap_key in snapshot and isinstance(comment_field, dict):
                    snapshot[snap_key]["comment"] = comment_field
    except urllib.error.HTTPError as exc:
        if exc.code == 410:
            log.error(
                "fetch_snapshot: comment enrichment hit HTTP 410 GONE — the Jira "
                "search endpoint has been RETIRED; snapshot written without "
                "comment data (per-ticket fallback applies). API retirement, not "
                "a transient fault: %r",
                exc,
            )
        else:
            log.warning(
                "fetch_snapshot: comment enrichment failed (HTTP %s: %r); "
                "snapshot written without comment data (per-ticket fallback)",
                exc.code,
                exc,
            )
    except BackendPaginationStallError:
        # A stalled comment-map read cannot supply an authoritative project view.
        raise
    except Exception as exc:  # noqa: BLE001 — fail-open: skip comment enrichment, per-ticket fallback
        log.warning(
            "fetch_snapshot: comment enrichment failed (%r); "
            "snapshot written without comment data (per-ticket fallback)",
            exc,
        )


def _enrich_issuelinks(client: TicketTransport, project_key: str, snapshot: dict, log) -> None:
    """Enrich one project in place. Pager stalls raise. Other faults defer."""
    # Read issue links in one paged project call because base search omits them.
    # Merge only returned lists. Failed enrichment remains unobserved and cannot
    # justify an inbound removal.
    try:
        if project_key and hasattr(client, "get_issuelinks_map"):
            # The merge lives in ``merge_issuelinks_map`` (ticket 6c0a) so the
            # key-present/key-absent observed contract it establishes is directly
            # testable (2b16's G1 guard drives it instead of hand-mirroring it).
            merge_issuelinks_map(snapshot, client.get_issuelinks_map(project_key))
    except urllib.error.HTTPError as exc:
        if exc.code == 410:
            log.error(
                "fetch_snapshot: issuelink enrichment hit HTTP 410 GONE — the Jira "
                "search endpoint has been RETIRED; snapshot written without "
                "issuelink data (degraded). API retirement, not a transient fault: %r",
                exc,
            )
        else:
            log.warning(
                "fetch_snapshot: issuelink enrichment failed (HTTP %s: %r); "
                "snapshot written without issuelink data (degraded)",
                exc.code,
                exc,
            )
    except BackendPaginationStallError:
        # A stalled pager means a truncated whole-project link map the differs would
        # read as an authoritative "no Jira links" (removals undetectable) — fail loud.
        raise
    except Exception as exc:  # noqa: BLE001 — fail-open: skip issuelink enrichment, write degraded snapshot
        log.warning(
            "fetch_snapshot: issuelink enrichment failed (%r); "
            "snapshot written without issuelink data (degraded)",
            exc,
        )


def _enrich_project(client: TicketTransport, project_key: str, snapshot: dict, log) -> dict:
    """Run parent, comment, and link enrichment for one project. Each fails open
    independently, while a pagination stall raises. Mutate and return the shared
    snapshot."""
    if not project_key and snapshot:
        first_key = next(iter(snapshot))
        project_key = first_key.rsplit("-", 1)[0] if "-" in first_key else ""
    if not project_key:
        return snapshot
    _enrich_parents(client, project_key, snapshot, log)
    _enrich_comments(client, project_key, snapshot, log)
    _enrich_issuelinks(client, project_key, snapshot, log)
    return snapshot


def _fetch_project(
    client: TicketTransport,
    queries: tuple[tuple[str, int | None], ...],
) -> list[dict]:
    """Drain active and recent-Done queries into one ordered project result. The caller
    merges only a complete result, so mid-page failure contributes no partial keys.
    Invalid project keys and truncation still fail closed."""
    issues: list[dict] = []
    for jql, cap in queries:
        issues.extend(drain(client, jql, page_size=100, cap=cap))
    return issues


def _build_snapshot(
    pass_id: str,
    repo_root: Path | None = None,
) -> dict:
    """Fetch all matching DIG issues across the two-JQL split and build the
    normalized snapshot dict — WITHOUT writing it to disk.

    Snapshot-BUILDING body shared by :func:`fetch_snapshot` (which writes the
    result) and :func:`compute_snapshot` (which returns it). For each mapped
    project it issues ``jql_active`` (``statusCategory != "Done"``) then
    ``jql_done_recent`` (Done category, ``ORDER BY updated DESC``, capped at
    ``_DONE_RECENT_CAP``); each paginates via ``_iter_pages`` and merges into one
    dict, deduped via ``seen_keys`` with a ``fetcher-dedup-suppressed`` alert.

    Raises:
        SilentTruncationError / BackendPaginationStallError: a truncated read on
            any query (ceiling hit, same-token-twice, or a stalled pager) —
            re-raised past the fail-open handlers (fail loud).
        A per-project transport error aborts only when a SINGLE project is mapped
            (fail-closed regression parity); with multiple projects it is isolated
            (see the base-query loop below).
    """
    if repo_root is None:
        repo_root = reconciler_repo_root()

    # S4: _load_acli returns the configured backend's transport directly (a
    # TicketTransport carrying its resolved connection settings).
    client = _load_acli()

    # Use the backend's configured query_project without a fallback. An absent or
    # invalid project must fail closed before JQL search.
    from rebar.config import compose_config
    from rebar.reconciler._backend_registry import select_backend

    _query_project = select_backend(compose_config()).query_project

    # projects.json selects synced projects. An unseeded store uses the configured
    # query project. Malformed mappings fail closed.
    from rebar.reconciler import projects_store

    project_list = list(projects_store.read_projects(repo_root).keys()) or [_query_project]

    # Load alert_store only after module initialization to avoid a cycle.
    alert_store = _load_alert_store()

    seen_keys: set[str] = set()
    snapshot: dict[str, dict] = {}

    import logging as _log_mod

    _fetcher_log = _log_mod.getLogger(__name__)

    # Build scoped JQL before project isolation. A transport failure drops only its
    # project without merging partial keys when other projects remain. A single-project
    # failure and any truncation still fail closed.
    _isolate_projects = len(project_list) > 1
    for _proj in project_list:
        project_queries: tuple[tuple[str, int | None], ...] = (
            (jql_active(_proj), None),
            (jql_done_recent(_proj), _DONE_RECENT_CAP),
        )
        try:
            project_issues = _fetch_project(client, project_queries)
        except (BackendPaginationStallError, SilentTruncationError):
            raise
        except Exception as exc:  # fail-open per project (mirrors _enrich_project)
            if not _isolate_projects:
                raise
            _fetcher_log.warning(
                "fetch_snapshot: base-query fetch for project %r failed (%r); "
                "SKIPPING it (issues NOT merged) and continuing the pass (%s)",
                _proj,
                exc,
                pass_id,
            )
            alert_store.append(
                {
                    "kind": "fetcher-project-fetch-failed",
                    "project": _proj,
                    "pass_id": pass_id,
                    "error": repr(exc),
                    "timestamp_ns": time.time_ns(),
                },
                repo_root=repo_root,
            )
            continue
        # Atomic merge — reached only when BOTH queries for this project succeeded.
        # Dedup (seen_keys) + the fetcher-dedup-suppressed alert run here.
        for issue in project_issues:
            key = issue.get("key", "")
            if not key:
                continue
            if key in seen_keys:
                alert_store.append(
                    {"kind": "fetcher-dedup-suppressed", "key": key, "pass_id": pass_id},
                    repo_root=repo_root,
                )
                continue
            seen_keys.add(key)
            fields = issue.get("fields", {})
            if not isinstance(fields, dict):
                fields = {}
            snapshot[key] = {k: fields[k] for k in sorted(fields.keys())}

    # Enrich each mapped project separately. Ordinary enrichment faults stay local.
    # A pagination stall raises.
    for _proj in project_list:
        _enrich_project(client, _proj, snapshot, _fetcher_log)
        enrich_local_id_properties(client, _proj, snapshot, _fetcher_log)

    # Proactive unmapped-status detection (defense-in-depth): flag a Jira status
    # the reconciler has no mapping for at snapshot-build time, before it reaches
    # an outbound mutation and trips the status preflight. Fully fail-open.
    try:
        _flag_unmapped_statuses(snapshot, pass_id, repo_root, alert_store, _fetcher_log)
    except Exception as exc:  # noqa: BLE001 — detection is best-effort; never fail the fetch
        _fetcher_log.warning("fetch_snapshot: unmapped-status detection failed (%r)", exc)

    return snapshot


def fetch_snapshot(
    pass_id: str,
    repo_root: Path | None = None,
) -> Path:
    """Fetch the normalized Jira snapshot and WRITE it to disk, returning the path.

    Builds the snapshot via :func:`_build_snapshot`, then writes a
    deterministically-ordered JSON file to
    ``.rebar/bridge/snapshots/<pass_id>.json`` and returns that path. External
    contract (Path return, on-disk file) is unchanged — ~18 callers/tests
    depend on it.
    """
    if repo_root is None:
        repo_root = reconciler_repo_root()

    snapshot = _build_snapshot(pass_id, repo_root)

    output_dir = snapshots_dir(repo_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{pass_id}.json"
    output_path.write_text(json.dumps(snapshot, sort_keys=True, indent=2))

    return output_path


def compute_snapshot(
    pass_id: str,
    repo_root: Path | None = None,
) -> dict:
    """Fetch the normalized Jira snapshot and RETURN it as a dict — writing NOTHING.

    Read-only counterpart to :func:`fetch_snapshot` for cap-0 (no-write) modes
    (dry-run/preview). Performs the identical fetch + merge +
    enrichment, but persists no snapshot file. The returned dict is byte-for-
    byte equivalent (after ``json.dumps(..., sort_keys=True)``) to what
    ``fetch_snapshot`` would have written, so the differ runs identically.
    """
    return _build_snapshot(pass_id, repo_root)
