#!/usr/bin/env python3
"""rebar Jira capability probe — six-step round-trip verification.

Verifies that all Jira operations required by the rebar Jira bridge (reconciler)
are functional: create, label, property-write, JQL-search, property-read, delete.

Run it as a preflight before relying on Jira sync (`rebar bridge check-access`). It
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

import sys

from rebar_reconciler.access_check import run_access_check
from rebar_reconciler.adapters.jira.acli import AcliClient

# ---------------------------------------------------------------------------
# Main probe
# ---------------------------------------------------------------------------


def main() -> None:
    _result, lines, returncode = run_access_check(client_cls=AcliClient)
    for line in lines:
        print(line)
    sys.exit(returncode)


if __name__ == "__main__":
    main()
