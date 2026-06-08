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
import os
import sys
import urllib.error
from pathlib import Path

# Split-JQL contract (bug f6cc-b174-9e9a-435c — single JQL hit 1000-issue
# ACLI ceiling because DIG has > 1000 issues across active + Done):
#
#   Query 1 (active working set): `project = <PROJ> AND status != "Done"`
#       The reconciler's primary scope — every issue we actively reconcile.
#       Empirically 1,050 issues on 2026-05-26 (probe run 26430555890),
#       headroom for moderate growth before the 1,200 ceiling triggers.
#
#   Query 2 (recent Done): `project = <PROJ> AND status = "Done" ORDER BY updated DESC`
#       Server-side sort + client-side cap at _DONE_RECENT_CAP. We capture
#       the most-recently-updated 1,000 Done issues; older Done items are
#       intentionally NOT in the snapshot. They remain in Jira but are
#       outside the bridge's reconciliation window.
#
# "Done" is the only Done-equivalent status in the DIG workflow (probe
# confirmed: To Do, In Progress, Done — no Closed / Resolved). If the
# DIG workflow adds a second closed-equivalent status, this list must
# extend OR the queries must move to `statusCategory != "Done"`.
JQL_ACTIVE = 'project = DIG AND status != "Done"'
JQL_DONE_RECENT = 'project = DIG AND status = "Done" ORDER BY updated DESC'

# Public JQL list (ordered: active first, then Done-recent). Exposed for
# observability tests that want to assert "the fetcher emitted these JQL
# strings". DO NOT export the single legacy `JQL` constant any more —
# bug f6cc replaced the single-query design.
JQLS: tuple[str, ...] = (JQL_ACTIVE, JQL_DONE_RECENT)

# Hard ACLI per-query ceiling. Raised from 1,000 to 1,200 in bug f6cc
# after empirical confirmation that the DIG working set has 1,050 active
# issues + 1,120 Done issues (probe 2026-05-26). 1,200 covers active
# with ~150-issue headroom and bounds the Done query under its 1,000-
# issue cap (see _DONE_RECENT_CAP). If either query exceeds this ceiling
# again, raise SilentTruncationError rather than silently truncating.
_ACLI_CEILING = 1200

# Cap on the Done snapshot — keep the N most-recently-updated Done issues
# only. ORDER BY updated DESC in JQL_DONE_RECENT ensures the cap selects
# the most-recently-updated items; older Done items are dropped at the
# fetch boundary (a documented trade-off in bug f6cc).
_DONE_RECENT_CAP = 1000


class SilentTruncationError(Exception):
    """Raised when ACLI silently truncates the result set.

    Two trigger conditions:
      * Accumulated issue count reaches the 1000-issue ACLI ceiling.
      * ACLI returns the same ``next_page_token`` on two consecutive calls
        ("same-token-twice" cursor-stall mode).
    """

    def __init__(self, message: str = "", reason: str = "") -> None:
        super().__init__(message or reason or "silent truncation detected")
        self.reason = reason


def _load_acli():
    """Load acli-integration module via importlib."""
    acli_path = Path(__file__).parent.parent / "acli-integration.py"
    spec = importlib.util.spec_from_file_location("acli_integration", acli_path)
    if spec is None:
        raise FileNotFoundError(f"acli-integration.py not found at {acli_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("acli_integration", mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# Canonical dotted key matching the codebase convention used by __main__'s
# _ADVISORY_LOCK_KEY / _MODE_KEY and applier's _MUTATION_KEY. Tests that
# patch `plugins.dso.scripts.dso_reconciler.alert_store.append` (e.g.
# test_fetcher_dedup_observable.py) target this key, so we MUST register
# the loaded module here so production and tests share a single module
# object. Choosing any other key would create a dual-load (Cluster A
# pattern), defeat existing patches, and reintroduce the bug class that
# bug ec9a-be6b-f50a-47b4 was filed to close.
_ALERT_STORE_KEY = "plugins.dso.scripts.dso_reconciler.alert_store"


def _load_alert_store():
    """Lazy-load alert_store under its canonical sys.modules key.

    Production callers (fetcher.fetch_snapshot dedup-alert path) need
    alert_store at runtime but cannot use `from plugins.dso.scripts...
    import alert_store` because `plugins` is not importable as a package
    in the production CI runner. This helper performs an importlib-based
    sibling load and registers under the canonical dotted key so any
    other loader / test patch sees the same module object.

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
        # for the next caller to reuse. Mirrors pre_cutover._load_step.
        sys.modules.pop(_ALERT_STORE_KEY, None)
        raise
    return mod


def _extract_issues(result) -> list[dict]:
    """Normalize a search_issues result to a list of issue dicts.

    ACLI stubs and the real client return either a bare list or a dict shaped
    ``{"issues": [...], "startAt": ..., "total": ...}``. Accept both.
    """
    if isinstance(result, dict):
        issues = result.get("issues", [])
        return list(issues) if isinstance(issues, list) else []
    if isinstance(result, list):
        return result
    return []


def _iter_pages(client, jql: str, page_size: int = 100, cap: int | None = None):
    """Generator yielding one page (list[dict]) per ACLI call.

    Termination:
      * Page is empty or shorter than ``page_size`` (natural end).
      * Accumulated issue count would meet/exceed the per-query ACLI
        ceiling — raises ``SilentTruncationError`` before yielding the
        violating page.
      * Caller-supplied ``cap`` is reached — stops cleanly (does NOT
        raise; the cap is an intentional client-side truncation, not a
        silent ACLI truncation). When set, the final yielded page is
        sliced so total yielded items never exceed ``cap``.
      * ACLI returns the same ``next_page_token`` on two consecutive calls
        ("same-token-twice") — raises
        ``SilentTruncationError(reason='same-token-twice')``.
    """
    start_at = 0
    accumulated = 0
    prev_token: object = None
    token_seen_count = 0
    while True:
        result = client.search_issues(jql, start_at=start_at, max_results=page_size)
        page = _extract_issues(result)

        # Same-token-twice cursor-stall detection. Inspect any of the common
        # token attribute names exposed by the client (POSIX-ish duck-typing).
        cur_token = None
        for attr in ("next_page_token", "nextPageToken"):
            if hasattr(client, attr):
                cur_token = getattr(client, attr)
                break
        if cur_token is not None and prev_token is not None and cur_token == prev_token:
            token_seen_count += 1
            if token_seen_count >= 1:
                raise SilentTruncationError(
                    "ACLI returned the same next_page_token twice in a row "
                    "(same-token-twice cursor stall)",
                    reason="same-token-twice",
                )
        else:
            token_seen_count = 0
        prev_token = cur_token

        if not page:
            return

        # Per-query ACLI ceiling: if adding this page would reach or exceed
        # the ceiling, raise rather than yield a silently-truncated set.
        if accumulated + len(page) >= _ACLI_CEILING:
            raise SilentTruncationError(
                f"ACLI working set reached the {_ACLI_CEILING}-issue ceiling "
                "(JRACLOUD-94632 silent truncation)",
                reason="ceiling",
            )

        # Client-side cap: yield a clipped final page if we'd exceed `cap`.
        if cap is not None and accumulated + len(page) > cap:
            remaining = cap - accumulated
            if remaining > 0:
                yield page[:remaining]
            return

        yield page
        accumulated += len(page)

        if cap is not None and accumulated >= cap:
            return

        if len(page) < page_size:
            return
        start_at += page_size


def collect(
    client, jql: str, page_size: int = 100, cap: int | None = None
) -> list[dict]:
    """Drain ``_iter_pages`` into a single flat list of issues."""
    issues: list[dict] = []
    for page in _iter_pages(client, jql, page_size=page_size, cap=cap):
        issues.extend(page)
    return issues


def fetch_snapshot(
    pass_id: str,
    repo_root: Path | None = None,
) -> Path:
    """Fetch all matching DIG issues across the two-JQL split and write a
    normalized snapshot JSON.

    Issues two queries in order (see ``JQLS``):

      1. ``JQL_ACTIVE``  — active working set (``status != "Done"``).
      2. ``JQL_DONE_RECENT`` — Done issues, ``ORDER BY updated DESC``,
         capped at ``_DONE_RECENT_CAP``.

    Each query paginates via ``_iter_pages``. Results are merged into a
    single snapshot dict; cross-query duplicates (which should not occur —
    status partitions the set — but are tolerated for robustness) are
    deduped via ``seen_keys`` and emit a ``fetcher-dedup-suppressed`` alert.

    Writes a deterministically-ordered JSON snapshot to
    ``bridge_state/snapshots/<pass_id>.json``.

    Raises:
        SilentTruncationError: Per-query ACLI ceiling hit, or same-token-
            twice cursor stall on either query.
        Any exception raised by ``AcliClient.search_issues()`` propagates out.
    """
    if repo_root is None:
        repo_root = Path(os.environ.get("REBAR_ROOT") or os.environ.get("PROJECT_ROOT") or Path(__file__).resolve().parents[4])

    acli_mod = _load_acli()
    client = acli_mod.AcliClient(
        jira_url=os.environ.get("JIRA_URL", ""),
        user=os.environ.get("JIRA_USER", ""),
        api_token=os.environ.get("JIRA_API_TOKEN", ""),
    )

    # Lazy load to avoid a circular at module-load time (alert_store is leaf).
    alert_store = _load_alert_store()

    seen_keys: set[str] = set()
    snapshot: dict[str, dict] = {}

    # Per-query caps: active is uncapped (the ACLI ceiling is its only
    # bound); Done is intentionally capped to the most-recently-updated
    # _DONE_RECENT_CAP issues. Stored as a tuple of (jql, cap) so the
    # iteration is straightforward and observable.
    queries: tuple[tuple[str, int | None], ...] = (
        (JQL_ACTIVE, None),
        (JQL_DONE_RECENT, _DONE_RECENT_CAP),
    )

    for jql, cap in queries:
        for page in _iter_pages(client, jql, page_size=100, cap=cap):
            for issue in page:
                key = issue.get("key", "")
                if not key:
                    continue
                if key in seen_keys:
                    # Cross-page (or cross-query) duplicate — dedup AND
                    # emit observable alert.
                    alert_store.append(
                        {
                            "kind": "fetcher-dedup-suppressed",
                            "key": key,
                            "pass_id": pass_id,
                        },
                        repo_root=repo_root,
                    )
                    continue
                seen_keys.add(key)
                fields = issue.get("fields", {})
                if not isinstance(fields, dict):
                    fields = {}
                snapshot[key] = {k: fields[k] for k in sorted(fields.keys())}

    # Parent enrichment (ticket 8b25-ae7a-efc3-47f6):
    # ACLI's -f field selector silently rejects the ``parent`` field, so
    # the snapshot entries built from search_issues() above never carry a
    # parent key.  We perform ONE extra paged REST search via
    # client.get_parent_map() to retrieve {key → parent_key|None} for the
    # full project scope, then merge the parent field into each snapshot entry.
    #
    # Degradation contract: get_parent_map logs a warning and returns {} on
    # any REST failure; the snapshot is still written without parent data so
    # the reconciler pass completes rather than blocking on a transient error.
    import logging as _log_mod

    _fetcher_log = _log_mod.getLogger(__name__)
    try:
        # Derive the project key from the first snapshot key (e.g. "DIG-123" → "DIG").
        # Fall back to the JIRA_PROJECT env var when the snapshot is empty.
        project_key = os.environ.get("JIRA_PROJECT", "")
        if not project_key and snapshot:
            first_key = next(iter(snapshot))
            project_key = first_key.rsplit("-", 1)[0] if "-" in first_key else ""
        if project_key and hasattr(client, "get_parent_map"):
            parent_map = client.get_parent_map(project_key)
            for snap_key, parent_jira_key in parent_map.items():
                if snap_key in snapshot:
                    if parent_jira_key:
                        snapshot[snap_key]["parent"] = {"key": parent_jira_key}
                    # When parent_jira_key is None, leave the field absent
                    # (top-level issue) — consistent with Jira REST shape.
    except urllib.error.HTTPError as exc:
        # API retirements (HTTP 410 GONE) must be loud — a transient WARNING
        # would let a permanent endpoint removal hide in the noise. Transient
        # HTTP faults stay at WARNING (ticket 8b25). get_parent_map already
        # swallows 410 internally; this catch is the defense-in-depth net for
        # any 410 that surfaces from a future enrichment path.
        if exc.code == 410:
            _fetcher_log.error(
                "fetch_snapshot: parent enrichment hit HTTP 410 GONE — the Jira "
                "search endpoint has been RETIRED; snapshot written without parent "
                "data (degraded). API retirement, not a transient fault: %r",
                exc,
            )
        else:
            _fetcher_log.warning(
                "fetch_snapshot: parent enrichment failed (HTTP %s: %r); "
                "snapshot written without parent data (degraded)",
                exc.code,
                exc,
            )
    except Exception as exc:  # noqa: BLE001
        _fetcher_log.warning(
            "fetch_snapshot: parent enrichment failed (%r); "
            "snapshot written without parent data (degraded)",
            exc,
        )

    # Comment-state enrichment (Action viability): the per-commented-ticket
    # ``acli comment list`` calls the differ would otherwise issue every pass
    # (~1-2s each, fleet-wide) are amortised into ONE paged REST search via
    # client.get_comment_map(). We merge the returned ``comment`` field into
    # each snapshot entry so outbound_differ._diff_comments takes the
    # snapshot-carried path (no client.get_comments round-trip).
    #
    # Invariant: only entries the search actually returned a comment field for
    # are enriched; entries the search omits keep NO ``comment`` key, so the
    # differ falls back to the per-ticket get_comments path for them (the
    # never-emit-blind safety invariant stays intact). On any search failure
    # the enrichment is skipped entirely and every ticket falls back — the
    # reconciler pass still completes.
    try:
        if project_key and hasattr(client, "get_comment_map"):
            comment_map = client.get_comment_map(project_key)
            for snap_key, comment_field in comment_map.items():
                if snap_key in snapshot and isinstance(comment_field, dict):
                    snapshot[snap_key]["comment"] = comment_field
    except urllib.error.HTTPError as exc:
        if exc.code == 410:
            _fetcher_log.error(
                "fetch_snapshot: comment enrichment hit HTTP 410 GONE — the Jira "
                "search endpoint has been RETIRED; snapshot written without "
                "comment data (per-ticket fallback applies). API retirement, not "
                "a transient fault: %r",
                exc,
            )
        else:
            _fetcher_log.warning(
                "fetch_snapshot: comment enrichment failed (HTTP %s: %r); "
                "snapshot written without comment data (per-ticket fallback)",
                exc.code,
                exc,
            )
    except Exception as exc:  # noqa: BLE001
        _fetcher_log.warning(
            "fetch_snapshot: comment enrichment failed (%r); "
            "snapshot written without comment data (per-ticket fallback)",
            exc,
        )

    output_dir = repo_root / "bridge_state" / "snapshots"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{pass_id}.json"
    output_path.write_text(json.dumps(snapshot, sort_keys=True, indent=2))

    return output_path
