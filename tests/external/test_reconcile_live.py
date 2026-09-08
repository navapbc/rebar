"""External validation of a non-destructive Jira bridge preview.

The test requires the external opt-in, Jira credentials, and ``acli``. A ``bridge_preview``
against the service must return a well-formed differ plan with ``mutations_applied == 0`` and
``no_write`` set. Integration tests own detailed field-fidelity coverage.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

import rebar

pytestmark = pytest.mark.external


def _live_jira_ready() -> bool:
    """True when live Jira creds AND the acli binary are both present."""
    creds = all(os.environ.get(k) for k in ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN"))
    return creds and shutil.which("acli") is not None


_skip = pytest.mark.skipif(not _live_jira_ready(), reason="no live Jira creds / acli binary")

# The well-formed-plan contract: every dry-run result must carry these keys, and
# every plan entry must carry these (mirrors reconcile_helpers._build_plan_entries).
_RESULT_KEYS = {"pass_id", "mutation_count", "mutations_applied", "mutation_failures"}
_PLAN_ENTRY_KEYS = {"direction", "action", "target", "local_id"}
_VALID_DIRECTIONS = {"outbound", "inbound", ""}


@_skip
def test_bridge_preview_plan_is_non_destructive_and_well_formed(rebar_repo: Path) -> None:
    result = rebar.bridge_preview(repo_root=str(rebar_repo))
    assert result["route"] == "preview"
    details = result["details"]

    # Non-destructive: preview is cap-0, so it must apply nothing.
    assert details.get("no_write") is True, f"preview did not report no_write: {result}"
    assert details.get("mutations_applied", 0) == 0, (
        f"preview APPLIED mutations — not non-destructive: {result}"
    )
    assert details.get("manifest_path") is None, (
        f"preview wrote a manifest (destructive side effect): {result}"
    )

    # Well-formed: the details envelope carries the documented keys.
    assert _RESULT_KEYS <= set(details), f"preview details missing keys: {result}"
    assert isinstance(details["mutation_count"], int)

    # Well-formed plan: the field set matches what the differ produces — every
    # entry has the same {direction, action, target, local_id} shape the mock
    # differ's plan entries carry, with a recognised direction.
    plan = details.get("plan", [])
    assert isinstance(plan, list)
    assert len(plan) == details["mutation_count"], (
        f"plan length {len(plan)} != mutation_count {details['mutation_count']}"
    )
    for entry in plan:
        assert _PLAN_ENTRY_KEYS <= set(entry), f"malformed plan entry: {entry}"
        assert entry["direction"] in _VALID_DIRECTIONS, f"unknown direction: {entry}"
        assert isinstance(entry["action"], str) and entry["action"], (
            f"plan entry missing action: {entry}"
        )
