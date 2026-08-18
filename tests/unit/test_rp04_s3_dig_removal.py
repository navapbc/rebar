"""Happy-path oracle for RP-04 S3 DIG-default elimination (ticket 6e3b).

AC2: no request (write OR live probe) uses the implicit ``"DIG"`` Jira project unless
``JIRA_PROJECT`` / ``jira.project`` is EXPLICITLY ``"DIG"``. This file pins the positive
half of that invariant — an explicitly-configured project is used verbatim. The negative
half (each unset site fails closed with a typed/redacted error and no ``DIG`` request)
lives in a held-out oracle the implementer does not see.

Observable behavior only: returned scope values and the arguments a recording transport
is constructed with.
"""

from __future__ import annotations

from rebar_reconciler import runtime as rt


def test_resolve_provider_scope_uses_explicit_cloud_project_verbatim(monkeypatch) -> None:
    """An explicitly-configured Cloud project is the write AND read scope, verbatim."""
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    _auth, project, query_project = rt._resolve_provider_scope("jira", "REB")
    assert project == "REB"
    assert query_project == "REB"


def test_resolve_provider_scope_passes_explicit_dig_through_when_configured(monkeypatch) -> None:
    """When the operator EXPLICITLY sets project=DIG, DIG is honored (not forbidden)."""
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    _auth, project, query_project = rt._resolve_provider_scope("jira", "DIG")
    assert project == "DIG"
    assert query_project == "DIG"


def test_datacenter_scope_uses_configured_project_verbatim(monkeypatch) -> None:
    """Data Center write/read scope is the configured project verbatim (no default)."""
    monkeypatch.setenv("JIRA_PAT", "pat")
    _auth, project, query_project = rt._resolve_provider_scope("jira-datacenter", "OPS")
    assert project == "OPS"
    assert query_project == "OPS"
