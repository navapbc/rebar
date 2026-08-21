#!/usr/bin/env python3
"""Fetcher: pull a normalized Jira snapshot and write it to bridge_state/snapshots/.

fetch_snapshot(pass_id) calls AcliClient.search_issues() with the filtered JQL,
paginates through the working set via ``_iter_pages``, dedups cross-page
duplicates while emitting an observable alert, enforces the 1000-issue ACLI
ceiling by raising ``SilentTruncationError``, and writes the normalized snapshot
as sorted-key JSON to bridge_state/snapshots/<pass_id>.json.

Two fetches over identical remote data produce byte-identical files (idempotent).
"""

from __future__ import annotations

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
    # resolve it (a `rebar_reconciler.`-qualified import widens to Any under
    # ignore_missing_imports). TYPE_CHECKING-only, so the standalone
    # importlib-by-path load — where a relative runtime import has no package —
    # is unaffected.
    from ._backend import TicketTransport

# Ticket 18a4: named ABSOLUTELY (the established pattern here — cf.
# ``outbound_differ``'s ``from rebar_reconciler._loader import lazy_load``) because
# this module is also loaded standalone via ``importlib.util.spec_from_file_location``,
# where a RELATIVE runtime import has no package to resolve against.
from rebar.config import repo_root_env
from rebar_reconciler._backend import BackendPaginationStallError
from rebar_reconciler.fetch_paging import (  # noqa: F401
    _ACLI_CEILING,
    SilentTruncationError,
    _extract_issues,
    _iter_pages,
    collect,
)

# Split-JQL contract (bug f6cc-b174-9e9a-435c — single JQL hit 1000-issue
# ACLI ceiling because DIG has > 1000 issues across active + Done):
#
#   Query 1 (active working set): `project = <PROJ> AND statusCategory != "Done"`
#       The reconciler's primary scope — every issue we actively reconcile.
#       Empirically 1,050 issues on 2026-05-26 (probe run 26430555890),
#       headroom for moderate growth before the 1,200 ceiling triggers.
#
#   Query 2 (recent Done): `project = <PROJ> AND statusCategory = "Done" ORDER BY updated DESC`
#       Server-side sort + client-side cap at _DONE_RECENT_CAP. We capture
#       the most-recently-updated 1,000 Done issues; older Done items are
#       intentionally NOT in the snapshot. They remain in Jira but are
#       outside the bridge's reconciliation window.
#
# The inbound search JQL is scoped to the CONFIGURED jira.project, built per
# pass from the resolved project key (see ``_build_snapshot``). It was previously
# hardcoded to ``project = DIG`` (bug 626d): the reconciler fetched DIG's issues
# regardless of ``[jira] project`` / ``JIRA_PROJECT``, so re-pointing the bridge at
# a different project still pulled (and tried to mutate) the wrong project. The
# builders below derive the project from config; an absent/invalid project key is
# rejected (fail-closed) rather than silently searching all projects.
#
# The active/done split is scoped by Jira's built-in ``statusCategory`` meta-status
# (To Do / In Progress / Done), not a literal status NAME, so a client's custom Closed
# / Resolved / Cancelled status (all in the Done category) scopes right (ticket 7332).
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


# Cap on the Done snapshot — keep the N most-recently-updated Done issues
# only. ORDER BY updated DESC in jql_done_recent() ensures the cap selects
# the most-recently-updated items; older Done items are dropped at the
# fetch boundary (a documented trade-off in bug f6cc).
_DONE_RECENT_CAP = 1000


def _load_acli():
    """Return the configured backend's transport (a ``TicketTransport``, i.e. an
    ``AcliClient``) directly — routed through the Backend port (S4).

    Lazily imports ``load_config``/``select_backend`` to avoid import cycles and to
    keep standalone by-path loading working.
    """
    from rebar.config import compose_config
    from rebar_reconciler._backend_registry import select_backend

    return select_backend(compose_config()).transport


# Canonical dotted key matching the codebase convention used by __main__'s
# _ADVISORY_LOCK_KEY / _MODE_KEY and applier's _MUTATION_KEY. Tests that
# patch `rebar_reconciler.alert_store.append` (e.g.
# test_fetcher_dedup_observable.py) target this key, so we MUST register
# the loaded module here so production and tests share a single module
# object. Choosing any other key would create a dual-load (Cluster A
# pattern), defeat existing patches, and reintroduce the bug class that
# bug ec9a-be6b-f50a-47b4 was filed to close.
_ALERT_STORE_KEY = "rebar_reconciler.alert_store"


def _load_alert_store():
    """Lazy-load alert_store under its canonical sys.modules key.

    Production callers (fetcher.fetch_snapshot dedup-alert path) need
    alert_store at runtime. This helper performs an importlib-based sibling
    load and registers it under the canonical ``rebar_reconciler.alert_store``
    dotted key so any other loader / test patch sees the same module object.

    On exec_module failure, the partially-initialised module is removed
    from sys.modules before re-raising so a subsequent call retries
    cleanly rather than reusing a broken module (copilot review finding
    on PR #363).
    """
    if _ALERT_STORE_KEY in sys.modules:
        return sys.modules[_ALERT_STORE_KEY]
    alert_store_path = Path(__file__).parent / "alert_store.py"
    spec = importlib.util.spec_from_file_location(_ALERT_STORE_KEY, alert_store_path)
    if spec is None or spec.loader is None:
        raise ImportError(
            f"Cannot load alert_store from {alert_store_path} — "
            f"spec_from_file_location returned spec={spec!r}"
        )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_ALERT_STORE_KEY] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        # Cleanup: don't leave a half-initialised module in sys.modules
        # for the next caller to reuse. Mirrors the sibling-loader pattern.
        sys.modules.pop(_ALERT_STORE_KEY, None)
        raise
    return mod


def _known_jira_statuses(repo_root: Path) -> frozenset[str]:
    """Jira workflow status NAMES the reconciler can round-trip: the built-in
    reverse-map keys (``config.jira_to_local_status``) UNION every Jira status NAME the
    mapping config for ``repo_root`` declares (``mapping_config.declared_status_names``).
    An empty BUILT-IN mapping disables the check (the preflight kill-switch)."""
    from rebar_reconciler import config as _cfg
    from rebar_reconciler import mapping_config as _mc

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
    """Merge a ``{jira_key → parent_key | None}`` map into the snapshot entries.

    THE THREE-STATE CONTRACT this function exists to establish (ticket 88d9). An
    inbound parent CLEAR is a write that DESTROYS local data, so the differ may only
    fire it on POSITIVE evidence that Jira was asked and answered "no parent" — never
    on the mere absence of data. That distinction has to be made HERE, because this is
    the only place that knows whether a key was queried:

      * the map does NOT mention the key  → no ``parent`` key is written at all. This is
        the UNOBSERVED case: a truncated page walk, a cross-project issue, a client with
        no ``get_parent_map``, or the whole-map ``{}`` degradation
        (``jira_datacenter/transport.py`` logs a warning and returns ``{}`` on ANY REST
        failure). Downstream must never read a clear out of it.
      * the map maps the key to a TRUTHY parent key → ``{"key": <parent>}`` as before.
      * the map maps the key to None → ``"parent": None``, i.e. the key is PRESENT with a
        falsy value. This is "queried, and Jira genuinely has no parent" — the only shape
        that authorises a clear.

    Before 88d9 the None case deliberately left the field ABSENT ("consistent with Jira
    REST shape"), which collapsed it into the unobserved case and made a de-parented issue
    indistinguishable from one that never had a parent — so an inbound de-parenting was
    invisible to rebar forever.

    Extracted from the inline merge in ``fetch_snapshot`` so the guard tests can drive the
    PRODUCTION merge directly instead of hand-mirroring it (ticket 2b16's
    ``_enrich_like_fetcher`` was exactly the byte-faithful copy this avoids — deleted by
    ticket 6c0a in favour of :func:`merge_issuelinks_map`: it guarded the
    boundary between a truncated read and deleted local data, and it silently drifted).

    Mutates and returns ``snapshot``.
    """
    for snap_key, parent_jira_key in parent_map.items():
        if snap_key not in snapshot:
            continue
        snapshot[snap_key]["parent"] = {"key": parent_jira_key} if parent_jira_key else None
    return snapshot


def merge_issuelinks_map(
    snapshot: dict[str, dict],
    issuelinks_map: dict[str, Any],
) -> dict[str, dict]:
    """Merge a ``{jira_key → issuelinks list}`` map into the snapshot entries.

    THE KEY-PRESENCE CONTRACT this function exists to pin (ticket 6c0a; the
    issuelinks sibling of :func:`merge_parent_map`). An inbound link REMOVAL is a
    write that DESTROYS local data, so 2b16's guard G1 may only let it fire on
    POSITIVE evidence that the peer was queried and answered — never on the mere
    absence of data. That distinction is made HERE, by key presence:

      * the map does NOT mention the key → no ``issuelinks`` key is written at
        all. This is the UNOBSERVED case: a truncated page walk, a failed or
        skipped enrichment (HTTP 410, fail-open partial map), a cross-project
        issue, or a client with no ``get_issuelinks_map``. Downstream must never
        read a removal out of it.
      * the map maps the key to a list (possibly ``[]``) → the entry gets
        ``"issuelinks": <list>``. An empty list is an AUTHORITATIVE "queried,
        and Jira genuinely has no links" — the only shape that authorises a
        removal.
      * a non-list value is not an observation: the key stays absent.

    Extracted from the inline merge in ``_build_snapshot`` so the guard tests can
    drive the PRODUCTION merge directly instead of hand-mirroring it (ticket
    2b16's ``_enrich_like_fetcher`` was exactly the byte-faithful copy this
    deletes: if the merge drifted so key-presence stopped meaning "observed",
    the copy would keep G1's tests green while the removal path started deleting
    local deps from truncated reads).

    Mutates and returns ``snapshot``.
    """
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
        # API retirements (HTTP 410 GONE) must be loud; transient HTTP faults stay
        # at WARNING (ticket 8b25). get_parent_map already swallows 410 internally —
        # this catch is the defense-in-depth net for any 410 from a future path.
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
        # A stalled pager means a truncated whole-project map the differ would treat
        # as authoritative (every missing parent reads as parentless) — fail loud.
        raise
    except Exception as exc:  # noqa: BLE001 — fail-open: skip parent enrichment, write degraded snapshot
        log.warning(
            "fetch_snapshot: parent enrichment failed (%r); "
            "snapshot written without parent data (degraded)",
            exc,
        )


def _enrich_comments(client: TicketTransport, project_key: str, snapshot: dict, log) -> None:
    """Comment-state enrichment for ONE project (story 1734 fan-out helper).
    Fail-open; BackendPaginationStallError re-raises. Mutates ``snapshot``."""
    # Comment-state enrichment (Action viability): amortise the per-ticket
    # ``acli comment list`` calls into ONE paged REST search via
    # client.get_comment_map(), merging the ``comment`` field so
    # outbound_differ._diff_comments takes the snapshot-carried path. Only entries
    # the search returned a comment field for are enriched; the rest keep NO
    # ``comment`` key and fall back to the per-ticket path (never-emit-blind
    # invariant intact). Any failure skips enrichment entirely — the pass completes.
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
        # A stalled pager means a truncated whole-project comment map — fail loud.
        raise
    except Exception as exc:  # noqa: BLE001 — fail-open: skip comment enrichment, per-ticket fallback
        log.warning(
            "fetch_snapshot: comment enrichment failed (%r); "
            "snapshot written without comment data (per-ticket fallback)",
            exc,
        )


def _enrich_issuelinks(client: TicketTransport, project_key: str, snapshot: dict, log) -> None:
    """Issuelink enrichment for ONE project (story 1734 fan-out helper). Fail-open;
    BackendPaginationStallError re-raises. Mutates ``snapshot`` in place."""
    # Issuelink enrichment (bug 3f04): the base search omits issuelinks, so the
    # inbound link differ and the outbound dedup both saw zero Jira links. Amortise
    # into ONE paged REST search via client.get_issuelinks_map() and merge the
    # array into each entry. Only entries the search returned a list for are
    # enriched; on any failure enrichment is skipped (differs degrade to "no Jira
    # links" — ADD-only sync stays safe) and the pass completes.
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
    """Run the three fail-open enrichment passes (parent / comment / issuelink)
    for ONE project against the shared ``snapshot`` (story 1734 fan-out).

    Called once per mapped project by ``_build_snapshot``. Each pass fails open
    independently (logs + degrades) while a ``BackendPaginationStallError``
    re-raises out of all three (a stalled pager is a truncated whole-project
    read — fail loud). ``project_key`` is the project to enrich; the
    snapshot-derived fallback is preserved for the single-project path where the
    configured key is absent. Mutates + returns ``snapshot``.
    """
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
    """Drain BOTH base queries (active + done-recent) for ONE project into one
    ordered list of raw issues (story 1734 fan-out helper, symmetric to
    ``_enrich_project``). The caller merges the result ONLY on success, so a
    mid-pagination failure leaves NO partial keys behind; ``queries`` is built
    outside the caller's boundary so an invalid key still fails closed in
    ``jql_active`` (bug 626d) and truncation signals propagate.
    """
    issues: list[dict] = []
    for jql, cap in queries:
        for page in _iter_pages(client, jql, page_size=100, cap=cap):
            issues.extend(page)
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
        repo_root = Path(repo_root_env() or Path(__file__).resolve().parents[4])

    # S4: _load_acli returns the configured backend's transport directly (a
    # TicketTransport carrying its resolved connection settings).
    client = _load_acli()

    # Resolve the JQL-scoping project via the Backend port's ``query_project``
    # (ticket 97f2/bbf1) — the UN-defaulted configured project: an absent/invalid
    # value must raise in jql_active() to fail the pass closed rather than search
    # unscoped (so we do NOT read client.jira_project, which defaults to "DIG").
    from rebar.config import compose_config
    from rebar_reconciler._backend_registry import select_backend

    _query_project = select_backend(compose_config()).query_project

    # Multi-project fan-out (story 1734): projects.json is the authoritative sync
    # list. An UNSEEDED store falls back to ``[_query_project]`` (pre-1734
    # single-project parity); a malformed mapping fails closed in read_projects().
    from rebar_reconciler import projects_store

    project_list = list(projects_store.read_projects(repo_root).keys()) or [_query_project]

    # Lazy load to avoid a circular at module-load time (alert_store is leaf).
    alert_store = _load_alert_store()

    seen_keys: set[str] = set()
    snapshot: dict[str, dict] = {}

    import logging as _log_mod

    _fetcher_log = _log_mod.getLogger(__name__)

    # Base-query fan-out (story 1734) with PER-PROJECT resilience (bug 05b8): the
    # (jql, cap) pair is built OUTSIDE the per-project try so an invalid key still
    # fails CLOSED in jql_active() (bug 626d). Each project drains into a LOCAL list
    # via _fetch_project and merges ONLY on success, so a transport fault degrades
    # only THAT project (alert + CONTINUE, mirroring the enrichment loop) with no
    # partial read reaching the snapshot as deletions; a truncation signal still
    # re-raises. The single-project path (one key / unseeded fallback) has no OTHER
    # project to protect, so it fails CLOSED unchanged (no empty snapshot).
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

    # Enrichment fan-out (story 1734): one parent/comment/issuelink pass PER
    # mapped project, each failing open independently; a BackendPaginationStallError
    # still re-raises out of the helper (a stalled pager is a truncated read).
    for _proj in project_list:
        _enrich_project(client, _proj, snapshot, _fetcher_log)

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
    ``bridge_state/snapshots/<pass_id>.json`` and returns that path. External
    contract (Path return, on-disk file) is unchanged — ~18 callers/tests
    depend on it.
    """
    if repo_root is None:
        repo_root = Path(repo_root_env() or Path(__file__).resolve().parents[4])

    snapshot = _build_snapshot(pass_id, repo_root)

    output_dir = repo_root / "bridge_state" / "snapshots"
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
    (dry-run / reconcile-check). Performs the identical fetch + merge +
    enrichment, but persists no snapshot file. The returned dict is byte-for-
    byte equivalent (after ``json.dumps(..., sort_keys=True)``) to what
    ``fetch_snapshot`` would have written, so the differ runs identically.
    """
    return _build_snapshot(pass_id, repo_root)
