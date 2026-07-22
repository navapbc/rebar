"""Bug 626d: the inbound search JQL must be scoped to the configured jira.project.

Pre-fix, ``fetcher`` hardcoded ``project = DIG`` in two module constants, so the
reconciler fetched DIG's issues regardless of ``[jira] project`` / ``JIRA_PROJECT``.
Re-pointing the bridge at another project (e.g. REB) still pulled — and tried to
mutate — DIG. Post-fix, the queries are built from the resolved project key via
``jql_active`` / ``jql_done_recent``; an absent/invalid key fails closed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
FETCHER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "fetcher.py"


def _load_fetcher():
    spec = importlib.util.spec_from_file_location("fetcher", FETCHER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fetcher"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fetcher():
    return _load_fetcher()


class _RecordingClient:
    """Records every JQL it is asked to search; returns no issues."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def search_issues(self, jql: str, start_at: int = 0, max_results: int = 50) -> list[dict]:
        self.calls.append({"jql": jql, "start_at": start_at, "max_results": max_results})
        return []


def _acli_module_returning(client):
    # S4: _load_acli returns the transport directly (not a module exposing
    # .AcliClient), so hand back the client itself.
    return client


# --- builder unit tests ----------------------------------------------------


def test_builders_scope_to_the_given_project(fetcher):
    assert fetcher.jql_active("REB") == 'project = REB AND status != "Done"'
    assert (
        fetcher.jql_done_recent("REB") == 'project = REB AND status = "Done" ORDER BY updated DESC'
    )
    assert "DIG" not in fetcher.jql_active("REB")
    assert fetcher.jqls_for("REB") == (
        fetcher.jql_active("REB"),
        fetcher.jql_done_recent("REB"),
    )


@pytest.mark.parametrize("bad", ["", "  ", "RE B", "1REB", "REB;DROP", "REB-1"])
def test_invalid_project_key_fails_closed(fetcher, bad):
    """An empty or malformed project key raises rather than searching unscoped
    (also blocks JQL injection via the project field)."""
    with pytest.raises(ValueError):
        fetcher.jql_active(bad)


# --- end-to-end: fetch_snapshot honors the configured project --------------


def test_fetch_snapshot_scopes_jql_to_configured_project(tmp_path, fetcher, monkeypatch):
    """With JIRA_PROJECT=REB, every issued JQL targets REB and none target DIG."""
    monkeypatch.setenv("JIRA_PROJECT", "REB")
    client = _RecordingClient()
    with patch.object(fetcher, "_load_acli", return_value=_acli_module_returning(client)):
        fetcher.fetch_snapshot("reb-scope-test", repo_root=tmp_path)

    assert client.calls, "fetch_snapshot must issue at least one search"
    issued = [c["jql"] for c in client.calls]
    assert all("project = REB" in jql for jql in issued), issued
    assert not any("DIG" in jql for jql in issued), f"must not query DIG: {issued}"
    assert client.calls[0]["jql"] == fetcher.jql_active("REB")
