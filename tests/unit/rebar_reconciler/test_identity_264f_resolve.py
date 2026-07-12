"""AC-coverage for 264f's `/user/search` bootstrap + no-mapping degradation criteria
(epic gnu-whale-ichor). Placed under tests/unit/rebar_reconciler/ for the package
conftest.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
import rebar_reconciler.outbound_differ as differ


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for a in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "d@e.com"),
        ("git", "config", "user.name", "d"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(a, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    # an identity with an email but NO jira accountId mapping yet
    rebar.create_identity("Ada", "ada@example.com", repo_root=str(repo))
    return repo


class _SearchClient:
    """Stub exposing search_user_by_email (one of _USER_SEARCH_METHODS)."""

    def __init__(self, result) -> None:
        self._result = result
        self.queried: list[str] = []

    def search_user_by_email(self, email: str):
        self.queried.append(email)
        return self._result


def test_user_search_bootstrap_resolves_transiently(store: Path) -> None:
    """An identity with an email but no jira mapping resolves via /user/search to the
    stubbed accountId (transient — never persisted to mappings)."""
    client = _SearchClient("acct-from-search")
    got = differ._bootstrap_account_id_via_user_search("ada@example.com", client)
    assert got == "acct-from-search"
    assert client.queried == ["ada@example.com"]
    # transient: the identity's mappings are unchanged (nothing persisted)
    idents = rebar.list_tickets(ticket_type="identity", repo_root=str(store))
    assert all(not t.get("mappings") for t in idents)


def test_user_search_bootstrap_miss_degrades(store: Path) -> None:
    """A /user/search miss (or ambiguous ≥2-match, which the REST helper collapses to
    None) degrades to None without raising."""
    assert (
        differ._bootstrap_account_id_via_user_search("ada@example.com", _SearchClient(None)) is None
    )
    # no client → None
    assert differ._bootstrap_account_id_via_user_search("ada@example.com", None) is None


def test_assignee_no_mapping_no_search_degrades_to_unassigned() -> None:
    """An assignee that resolves to no account (resolver → None) with an
    already-unassigned Jira issue degrades: no re-emit, no failure (convergence)."""
    ticket = {
        "ticket_id": "loc-1",
        "title": "T",
        "description": "D",
        "ticket_type": "task",
        "priority": 2,
        "status": "open",
        "assignee": "nobody",
    }

    def resolver(assignee, jira_key):  # (accountId|None, authoritative, is_account_id)
        return (None, True, False)

    changed = differ._diff_fields(
        ticket, {"fields": {"assignee": None}}, assignee_resolver=resolver, jira_key="REB-1"
    )
    assert "assignee" not in changed  # unmappable + already-unassigned → no churn
