"""Integration test for throwaway-DIG marker round-trip (story 10e3 / dd-3).

Skipped unless DSO_RECONCILER_INTEGRATION_DIG=1 and Jira credentials are
available. Asserts the new reconciler picks up a marker update on the next
pass and emits exactly one Mutation for the affected issue.
"""

from __future__ import annotations

import os

import pytest

INTEGRATION_ENV = os.environ.get("DSO_RECONCILER_INTEGRATION_DIG", "0")
JIRA_AVAILABLE = bool(os.environ.get("JIRA_API_TOKEN") or os.environ.get("JIRA_TOKEN"))

pytestmark = pytest.mark.skipif(
    INTEGRATION_ENV != "1" or not JIRA_AVAILABLE,
    reason="requires DSO_RECONCILER_INTEGRATION_DIG=1 and JIRA_API_TOKEN (or JIRA_TOKEN)",
)


def test_marker_round_trip_emits_exactly_one_mutation():
    """Insert a marker update on a throwaway DIG issue; next pass yields one Mutation."""
    pytest.skip(
        "Throwaway DIG integration scaffold - implementation requires a live "
        "DIG throwaway issue and the fetcher's pass-start hook. RED state: "
        "marked skip-on-test-bodies until the fetcher's marker-aware "
        "pass-start lands (task 71d4) and the throwaway-DIG fixture lands."
    )
    # Outline (when implementation lands):
    # 1. Fetch initial Jira state -> snapshot_0
    # 2. Update a marker field on a throwaway DIG issue (out-of-band via ACLI)
    # 3. Fetch again -> snapshot_1
    # 4. Call diff(local_state, snapshot_1) (or the new reconciler entry)
    # 5. Assert exactly one Mutation in result targeting the throwaway issue
    # 6. Assert the Mutation's payload reflects the marker update
