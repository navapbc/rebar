#!/usr/bin/env python3
"""rebar Jira capability probe — six-step round-trip verification.

Verifies that all Jira operations required by the rebar Jira bridge (reconciler)
are functional: create, label, property-write, JQL-search, property-read, delete.

Run it as a preflight before relying on Jira sync (`rebar bridge-probe`). It
creates a throwaway Jira issue and deletes it again in the same run.

Exit codes:
  0 — all six steps passed
  1 — one or more steps failed (but credentials were present)
  2 — missing credentials (JIRA_URL, JIRA_USER, or JIRA_API_TOKEN)

Environment variables:
  JIRA_URL        — Base URL of the Jira instance
  JIRA_USER       — Jira username (email for Jira Cloud)
  JIRA_API_TOKEN  — Jira API token
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Load acli-integration from the same directory (filename has hyphens)
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
_acli_path = _HERE / "acli-integration.py"
_acli_spec = importlib.util.spec_from_file_location("acli_integration", _acli_path)
if _acli_spec is None or _acli_spec.loader is None:
    raise ImportError(f"Cannot load acli-integration from {_acli_path}")
_acli_mod = importlib.util.module_from_spec(_acli_spec)
_acli_spec.loader.exec_module(_acli_mod)
AcliClient = _acli_mod.AcliClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Jira's label-indexing latency is eventually-consistent; empirical observation
# during the cfd6 live probe (2026-05-23) showed 4-second budget (3 attempts ×
# 2s sleep) consistently insufficient for fresh label visibility. Bumped to
# 6 × 5s = 30s total budget which matches Atlassian's documented indexing
# upper bound for label propagation on the DIG project. Bug 0b27-b785-dea8-49a0
# tracks the calibration.
_JQL_RETRY_COUNT = 6
_JQL_RETRY_SLEEP = 5  # seconds


# ---------------------------------------------------------------------------
# Main probe
# ---------------------------------------------------------------------------


def main() -> None:
    jira_url = os.environ.get("JIRA_URL", "")
    jira_user = os.environ.get("JIRA_USER", "")
    jira_api_token = os.environ.get("JIRA_API_TOKEN", "")
    # Project is configurable via env var for portability; default preserves
    # the in-tree DIG project used by the rebar Jira bridge.
    # `or "DIG"` (not the second arg to .get) so an explicit empty-string
    # JIRA_PROJECT="" — common when a templated secret renders blank — falls
    # back to the default rather than being passed through as an empty key.
    jira_project = os.environ.get("JIRA_PROJECT") or "DIG"

    if not jira_url or not jira_user or not jira_api_token:
        print("PROBE_FAIL reason=missing_credentials")
        sys.exit(2)

    probe_uuid = str(uuid.uuid4())
    label = f"rebar-id:{probe_uuid}"

    client = AcliClient(
        jira_url=jira_url,
        user=jira_user,
        api_token=jira_api_token,
        jira_project=jira_project,
    )

    issue_key: str | None = None
    failed = False

    try:
        # STEP 1: Create issue
        result = client.create_issue(
            {
                "title": f"rebar capability probe {probe_uuid}",
                "ticket_type": "task",
            }
        )
        issue_key = result.get("key") or result.get("id")
        if not issue_key:
            print(
                f"PROBE_FAIL step=STEP_CREATE reason=no_key_in_response detail={result!r}"
            )
            sys.exit(1)
        print("PROBE_PASS step=STEP_CREATE")

        # STEP 2: Add label (raw PUT — issue updates take {"update": ...}, not {"value": ...})
        client._direct_rest_put_raw(
            f"/rest/api/3/issue/{issue_key}",
            {"update": {"labels": [{"add": label}]}},
        )
        print("PROBE_PASS step=STEP_LABEL")

        # STEP 3: Write issue property
        client.set_issue_property(issue_key, "local_id", probe_uuid)
        print("PROBE_PASS step=STEP_PROPERTY_WRITE")

        # STEP 4: JQL search with retry. AcliClient.search_issues caches
        # results per-JQL (intentional, for the reconciler's pagination
        # loop), so the retry must explicitly invalidate the cache for this
        # JQL between attempts — otherwise the first empty result poisons
        # every subsequent retry and we never observe the freshly-indexed
        # label. Bug 0b27-b785-dea8-49a0 surfaced this via the cfd6 live probe.
        jql = f'labels="{label}"'
        results: list = []
        for _attempt in range(_JQL_RETRY_COUNT):
            _cache = getattr(client, "_search_cache", None)
            # Defensive: future AcliClient refactor (e.g. functools.lru_cache)
            # may not expose a dict; only attempt invalidation when the cache
            # is dict-like (pop method available).
            if isinstance(_cache, dict):
                _cache.pop(jql, None)
            results = client.search_issues(jql)
            if results:
                break
            if _attempt < _JQL_RETRY_COUNT - 1:
                time.sleep(_JQL_RETRY_SLEEP)

        if not results:
            print("PROBE_FAIL step=STEP_JQL_SEARCH reason=no_results_after_retry")
            failed = True
        else:
            print("PROBE_PASS step=STEP_JQL_SEARCH")

        # STEP 5: Read property back and verify. Catch KeyError separately so
        # a malformed-response signal (shape change in Jira's REST contract)
        # surfaces as a distinct PROBE_FAIL reason rather than being collapsed
        # into the catch-all `reason=exception` branch below.
        try:
            read_value = client.get_issue_property(issue_key, "local_id")
        except KeyError as exc:
            print(
                f"PROBE_FAIL step=STEP_PROPERTY_READ "
                f"reason=malformed_response detail={exc}"
            )
            failed = True
        else:
            if read_value != probe_uuid:
                print(
                    f"PROBE_FAIL step=STEP_PROPERTY_READ "
                    f"reason=value_mismatch expected={probe_uuid} got={read_value}"
                )
                failed = True
            else:
                print("PROBE_PASS step=STEP_PROPERTY_READ")

    except Exception as exc:  # noqa: BLE001
        print(f"PROBE_FAIL reason=exception detail={exc}")
        failed = True

    finally:
        # STEP 6: Delete (best-effort cleanup — always runs)
        if issue_key is not None:
            try:
                client.delete_issue(issue_key)
                print("PROBE_PASS step=STEP_DELETE")
            except Exception as exc:  # noqa: BLE001
                print(f"PROBE_FAIL step=STEP_DELETE reason=exception detail={exc}")
                failed = True

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
