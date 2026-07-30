"""Live coverage for the Data Center transport against the J5 harness (story J6,
epic e369).

Drives the REAL ``JiraDataCenterTransport`` (built on ``pycontribs/jira``) against
a real Jira 8.17.1 Data Center instance, asserting the SAME raw-shape contract the
unit tier asserts against a fake client (``tests/_jira_shape_contract.py`` — see
the execution-decision comment on ticket 9fd4-a94c-156e-4a56): if the DC transport
ever leaked a ``jira.Issue`` instead of a raw dict, both tiers would fail on a
shared assertion rather than a test-specific one.

Tier notes (inherited from ``tests/external/`` — see
``tests/external/live_jira_dc/test_harness_smoke.py``'s module docstring for the
full rationale, reproduced here only where it differs):

* the module-level ``_live_jira_ready`` sentinel below is what makes
  ``tests/external/conftest.py`` attach the ``jira_live`` marker and enrol this
  module in the all-skip canary;
* absent harness / missing ``[jira-datacenter]`` extra ⇒ SKIP with an actionable
  message, never a hard failure — the ``external`` CI job runs with no Docker and
  no Jira at all;
* every test here sets ``allow_insecure=true`` explicitly (the harness serves
  plain ``http://localhost:2990/jira``), so the loopback path exercises the
  config's TLS-override branch rather than bypassing the validator (epic AC13).
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from typing import Any

import pytest
from _jira_shape_contract import assert_comment_map_shape, assert_search_shape

_BASE = os.environ.get("JIRA_DC_BASE_URL", "http://localhost:2990/jira")


def _live_jira_ready() -> bool:
    """The sentinel ``tests/external/conftest.py`` keys on to apply ``jira_live``
    (enrolling this module in the all-skip canary) — also the readiness predicate
    for the ``skipif`` below."""
    try:
        req = urllib.request.Request(f"{_BASE.rstrip('/')}/rest/api/2/serverInfo")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _jira_extra_installed() -> bool:
    try:
        import jira  # noqa: F401
    except ImportError:
        return False
    return True


_skip = pytest.mark.skipif(
    not _live_jira_ready(),
    reason=(
        "Jira DC harness not reachable at "
        f"{_BASE} — start it with `make jira-dc-up` and run with REBAR_RUN_EXTERNAL=1"
    ),
)
_skip_no_extra = pytest.mark.skipif(
    not _jira_extra_installed(),
    reason="the 'jira-datacenter' extra (pycontribs/jira) is not installed — "
    "pip install 'nava-rebar[jira-datacenter]'",
)


@pytest.fixture
def dc_transport(jira_dc_pat: str) -> Any:
    """A REAL ``JiraDataCenterTransport`` against the live harness.

    Builds the client directly from harness fixtures (rather than through
    ``load_config()``) so this test suite does not depend on process-wide config
    discovery — ``allow_insecure=True`` mirrors what a ``[tool.rebar.reconciler]``
    config pointed at this loopback harness would need, exercised here as a
    direct constructor argument instead.
    """
    from rebar_reconciler.adapters.jira_datacenter.settings import JiraDataCenterSettings
    from rebar_reconciler.adapters.jira_datacenter.transport import (
        JiraDataCenterTransport,
        build_client_from_settings,
    )

    settings = JiraDataCenterSettings(
        url=_BASE,
        project="",  # overridden per-test via jira_dc_project
        allow_insecure=True,
        ca_bundle="",
        resolved_statuses=frozenset({"Resolved", "Done", "Cancelled"}),
        pat=jira_dc_pat,
    )
    client = build_client_from_settings(settings)
    return JiraDataCenterTransport(client=client, project="")


@_skip
@_skip_no_extra
def test_create_get_update_transition_roundtrip(
    dc_transport: Any, jira_dc_project: str, track_issue: Any
) -> None:
    """create -> read -> update -> transition-by-name, each asserted against the
    raw shape contract and its observable postcondition on the server."""
    dc_transport.project = jira_dc_project

    created = dc_transport.create_issue(
        {"summary": "rebar J6 live — roundtrip", "issuetype": "Task"}
    )
    assert isinstance(created, dict)
    key = created["key"]
    track_issue(key)

    fetched = dc_transport.get_issue(key)
    assert isinstance(fetched, dict)
    assert fetched["key"] == key
    assert fetched["fields"]["summary"] == "rebar J6 live — roundtrip"
    assert isinstance(fetched["fields"].get("description"), (str, type(None))), (
        "DC descriptions must be plain text, never an ADF dict"
    )

    updated = dc_transport.update_issue(key, summary="rebar J6 live — updated")
    assert updated["fields"]["summary"] == "rebar J6 live — updated"

    transitions = dc_transport._client.transitions(key)
    target = next((t["name"] for t in transitions if isinstance(t, dict) and t.get("name")), None)
    assert target is not None, f"no transitions available for {key}"
    dc_transport.transition_issue_by_name(key, target)
    after = dc_transport.get_issue(key)
    assert after["fields"]["status"]["name"] == target


@_skip
@_skip_no_extra
def test_transition_to_an_unavailable_name_raises(
    dc_transport: Any, jira_dc_project: str, track_issue: Any
) -> None:
    """A transition name the workflow does not offer raises rather than silently
    no-oping."""
    dc_transport.project = jira_dc_project
    created = dc_transport.create_issue(
        {"summary": "rebar J6 live — bad transition", "issuetype": "Task"}
    )
    key = created["key"]
    track_issue(key)

    with pytest.raises(ValueError):
        dc_transport.transition_issue_by_name(key, "definitely-not-a-real-status-name")


@_skip
@_skip_no_extra
def test_comment_and_search_shapes_match_the_shared_contract(
    dc_transport: Any, jira_dc_project: str, track_issue: Any
) -> None:
    dc_transport.project = jira_dc_project
    created = dc_transport.create_issue({"summary": "rebar J6 live — comment", "issuetype": "Task"})
    key = created["key"]
    track_issue(key)

    comment = dc_transport.add_comment(key, "a live comment")
    assert isinstance(comment, dict)

    assert_search_shape(dc_transport.search_issues(f"project = {jira_dc_project}"))
    assert_comment_map_shape(dc_transport.get_comment_map(jira_dc_project))


@_skip
@_skip_no_extra
def test_probe_remote_classifies_a_deleted_issue_as_archived_or_moved(
    dc_transport: Any, jira_dc_project: str
) -> None:
    """Absence-probe edge case: a deleted issue classifies as ARCHIVED_OR_MOVED,
    not merely "some error"."""
    from rebar_reconciler.inbound_probe import ProbeBranch

    dc_transport.project = jira_dc_project
    created = dc_transport.create_issue(
        {"summary": "rebar J6 live — to delete", "issuetype": "Task"}
    )
    key = created["key"]
    dc_transport._client.issue(key).delete()

    result = dc_transport.probe_remote(key)
    assert result.branch == ProbeBranch.ARCHIVED_OR_MOVED


@_skip
@_skip_no_extra
def test_select_backend_resolves_after_importing_adapters() -> None:
    """``select_backend("jira-datacenter")`` resolves once
    ``rebar_reconciler.adapters`` is imported — pinning the self-registration
    import (``adapters/__init__.py``) rather than a transport-only test that
    would never catch a missing registration."""
    import rebar_reconciler.adapters  # noqa: F401 — registers the DC factory
    from rebar_reconciler._backend_registry import _REGISTRY

    assert "jira-datacenter" in _REGISTRY
