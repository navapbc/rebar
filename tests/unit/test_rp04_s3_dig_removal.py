"""Verify that explicitly configured Jira project scopes pass through unchanged.

Cloud and Data Center resolution returns the configured project for write and query
scope, including when that explicit project is ``DIG``.
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
