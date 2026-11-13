"""Helpers for bounded Jira Cloud coordinator mutation probes.

Each created issue receives its coordinator binding label and a run-unique sweep label. The
owning test deletes the issue by key in ``finally``, with session and workflow label sweeps as
crash backstops. Pytest's ``prepend`` import mode exposes these helpers through the bare
``_cloud_mutation_support`` name.
"""

from __future__ import annotations

import os
import shutil
import time
import urllib.error
import uuid
from typing import Any

# Credentials the live-Cloud AcliClient needs; absence => skip (never a hard fail here).
_CLOUD_CRED_VARS = ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN")


def engine_on_path() -> None:
    """Put ``<repo>/src/rebar/_engine`` on ``sys.path`` so ``rebar.reconciler`` resolves.

    The reconciler ships stdlib-only UNDER the wheel rather than as a top-level install
    (mirrors the DC + S3 live harness helpers).
    """


def live_jira_ready() -> bool:
    """True iff live Jira creds AND the ``acli`` binary are present."""
    creds = all(os.environ.get(k) for k in _CLOUD_CRED_VARS)
    return bool(creds) and shutil.which("acli") is not None


def cloud_project() -> str:
    """The throwaway Cloud project to mutate (defaults to REB)."""
    return os.environ.get("JIRA_PROJECT", "REB")


def build_cloud_client(project: str | None = None) -> Any:
    """A live-Cloud ``AcliClient`` from JIRA_URL/JIRA_USER/JIRA_API_TOKEN.

    Build a FRESH client for any before/after visibility check: ``search_issues`` caches
    per-JQL PER instance, so a reused client could answer stale after a create.
    """
    engine_on_path()
    from rebar.reconciler.adapters.jira import acli

    return acli.AcliClient(
        jira_url=os.environ["JIRA_URL"],
        user=os.environ["JIRA_USER"],
        api_token=os.environ["JIRA_API_TOKEN"],
        jira_project=project or cloud_project(),
    )


# One label per process, so a local run's tests and its backstops agree on what to sweep.
_LOCAL_RUN_LABEL = f"rebar-id:cloudprobe-local-{uuid.uuid4().hex[:12]}"


def run_label() -> str:
    """The run-scoped sweep label.

    In CI the workflow exports ``REBAR_PROBE_RUN_LABEL=rebar-id:cloudprobe-<run_id>`` so the
    module and the workflow's teardown agree on the exact label to sweep. Locally (no such
    env) synthesize a unique one so a local run is still self-scoped and never collides with
    a concurrent probe.
    """
    label = os.environ.get("REBAR_PROBE_RUN_LABEL", "").strip()
    if label:
        return label
    return _LOCAL_RUN_LABEL


def new_local_id() -> str:
    """A globally-unique ``local_id`` for one throwaway issue.

    Embeds the run scope so a leaked issue's ``rebar-id:<local_id>`` label still reveals
    which run produced it, while the trailing uuid keeps the coordinator's observe() JQL
    (``labels = "rebar-id:<local_id>"``) resolving to exactly ONE issue.
    """
    base = run_label().removeprefix("rebar-id:")
    return f"{base}-{uuid.uuid4().hex[:12]}"


def _jql_backoff() -> list[float]:
    """The §1 capped-exponential JQL visibility schedule (reuses access_check)."""
    engine_on_path()
    from rebar.reconciler import access_check

    retries, base, cap = access_check._resolve_jql_backoff(os.environ)
    return access_check._jql_backoff_delays(retries, base, cap)


def wait_visible(client: Any, jql: str, *, sleep_fn=None) -> list[dict[str, Any]]:
    """Poll ``search_issues(jql)`` under the §1 backoff until it returns a hit.

    Jira Cloud's Lucene search index is eventually consistent: a create/label lands
    synchronously but can take seconds to become searchable. This reuses the SAME widened
    capped-exponential schedule the capability probe uses, so the live coordinator lifecycle
    is not flaky under index lag. Returns the (possibly empty) final result set.
    """
    sleep = sleep_fn or time.sleep
    delays = _jql_backoff()
    hits: list[dict[str, Any]] = []
    for attempt in range(len(delays) + 1):
        client.invalidate_search_cache()
        hits = client.search_issues(jql)
        if hits:
            return hits
        if attempt < len(delays):
            sleep(delays[attempt])
    return hits


def sweep_label(label: str) -> list[str]:
    """Best-effort delete of every live issue carrying *label*; return swept keys.

    The session/finally backstop for the primary by-key teardown. Never raises — a sweep
    failure must not mask the test result (the workflow's always-run acli sweep is the
    final net).
    """
    keys: list[str] = []
    try:
        client = build_cloud_client()
        client.invalidate_search_cache()
        hits = client.search_issues(f'labels = "{label}"')
    except Exception:  # noqa: BLE001 — best-effort backstop; must never mask the test result
        return keys
    for hit in hits:
        key = hit.get("key")
        if not key:
            continue
        try:
            client.delete_issue(key)
            keys.append(key)
        except Exception:  # noqa: BLE001 — one undeletable leftover must not abort the sweep
            continue
    return keys


def run_prefix() -> str:
    """The run-scoped name prefix for versions: the run label without ``rebar-id:``.

    ``cloudprobe-<run_id>`` in CI, ``cloudprobe-local-<hex>`` locally, so the version
    backstops can find exactly this run's versions by name.
    """
    return run_label().removeprefix("rebar-id:")


def version_name(label: str) -> str:
    """A unique, run-scoped version name (no commas: rebar parses release lists as CSV)."""
    return f"{run_prefix()}-{label}-{uuid.uuid4().hex[:8]}"


def create_version(client: Any, project: str, name: str) -> str:
    """Create version ``name`` in ``project`` over REST and return its id."""
    project_id = client._direct_rest_get(f"/rest/api/3/project/{project}")["id"]
    created = client._direct_rest_post_json(
        "/rest/api/3/version", {"name": name, "projectId": int(project_id)}
    )
    return str(created["id"])


def delete_version(client: Any, version_id: str) -> None:
    """Delete one version by id; an already-deleted version counts as deleted."""
    try:
        client._direct_rest_delete(f"/rest/api/3/version/{version_id}")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise


def sweep_prefixed_issues(prefix: str) -> list[str]:
    """Best-effort delete of REB issues whose summary contains ``prefix``; never raises.

    Covers the window between an issue's creation by a binding pass and the run label landing.
    """
    keys: list[str] = []
    try:
        client = build_cloud_client()
        client.invalidate_search_cache()
        hits = client.search_issues(f'project = "{cloud_project()}" AND summary ~ "\\"{prefix}\\""')
    except Exception:  # noqa: BLE001 — best-effort backstop; must never mask the test result
        return keys
    for hit in hits:
        key = hit.get("key")
        summary = str((hit.get("fields") or {}).get("summary", ""))
        if not key or prefix not in summary:
            continue
        try:
            client.delete_issue(key)
            keys.append(key)
        except Exception:  # noqa: BLE001 — one undeletable leftover must not abort the sweep
            continue
    return keys


def sweep_versions(prefix: str, project: str | None = None) -> list[str]:
    """Best-effort delete of every version in the project whose name starts with ``prefix-``.

    The version counterpart of :func:`sweep_label`. Never raises.
    """
    swept: list[str] = []
    try:
        client = build_cloud_client()
        versions = client._direct_rest_get(
            f"/rest/api/3/project/{project or cloud_project()}/versions"
        )
    except Exception:  # noqa: BLE001 — best-effort backstop; must never mask the test result
        return swept
    for version in versions or []:
        name = str(version.get("name", ""))
        if not name.startswith(f"{prefix}-") or not version.get("id"):
            continue
        try:
            delete_version(client, str(version["id"]))
            swept.append(name)
        except Exception:  # noqa: BLE001 — one undeletable leftover must not abort the sweep
            continue
    return swept
