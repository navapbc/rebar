"""Shared, bounded helpers for the live-Cloud coordinator MUTATION probe.

The sibling ``jira-cloud-s3-rehearsal`` suite is deliberately READ-ONLY on Jira Cloud, so
it cannot exercise the RP-03 create-coordinator's outbound WRITE paths (create / binding
lifecycle / commit-unknown / fuse) against real Cloud. This suite is the mutating
counterpart the epic's Live-External AC requires: it drives those exact seams against live
Jira Cloud, but under a hard self-cleaning contract so it can run against the shared REB
project without leaking.

SAFETY CONTRACT (every helper here upholds it):
  * every created issue is UNIQUELY labelled ``rebar-id:<local_id>`` (the coordinator's own
    binding label) AND stamped with the run-scoped ``REBAR_PROBE_RUN_LABEL`` so a crashed
    run's leftovers are sweepable by label;
  * the OWNING test deletes its issue by key in a ``finally`` (the primary teardown);
  * a session-level label sweep (conftest) and the workflow's always-run teardown step are
    backstops for a crash between create and delete.

Import-mode note: pytest's ``prepend`` mode puts this directory on ``sys.path``, so the
test module and conftest import these helpers by bare module name, mirroring the DC and
S3 live harnesses.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import rebar

# Credentials the live-Cloud AcliClient needs; absence => skip (never a hard fail here).
_CLOUD_CRED_VARS = ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN")


def engine_on_path() -> None:
    """Put ``<repo>/src/rebar/_engine`` on ``sys.path`` so ``rebar_reconciler`` resolves.

    The reconciler ships stdlib-only UNDER the wheel rather than as a top-level install
    (mirrors the DC + S3 live harness helpers).
    """
    engine_dir = Path(rebar.__file__).resolve().parent / "_engine"
    if str(engine_dir) not in sys.path:
        sys.path.insert(0, str(engine_dir))


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
    from rebar_reconciler.adapters.jira import acli

    return acli.AcliClient(
        jira_url=os.environ["JIRA_URL"],
        user=os.environ["JIRA_USER"],
        api_token=os.environ["JIRA_API_TOKEN"],
        jira_project=project or cloud_project(),
    )


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
    return f"rebar-id:cloudprobe-local-{uuid.uuid4().hex[:12]}"


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
    from rebar_reconciler import access_check

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
