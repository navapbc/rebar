"""AC-coverage for 2f13's inbound-wiring criterion (epic gnu-whale-ichor): applying an
inbound Jira assignee with an accountId mints/reuses a placeholder identity, and a
record missing an accountId falls back without failing. Placed under
tests/unit/rebar_reconciler/ so the package conftest makes the engine importable.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
import rebar_reconciler.apply_inbound_records as air


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
    return repo


def test_inbound_assignee_mints_placeholder_identity(store: Path) -> None:
    """An inbound assignee carrying an accountId mints a placeholder identity keyed
    on that accountId."""
    air._ensure_inbound_assignee_identity(
        {"accountId": "acct-inbound-1", "displayName": "Ghost Jira User"},
        repo_root=str(store),
    )
    ident = rebar.resolve_mapping("jira", "acct-inbound-1", repo_root=str(store))
    assert ident is not None
    assert rebar.is_placeholder(ident, repo_root=str(store)) is True
    assert rebar.show_ticket(ident, repo_root=str(store))["title"] == "Ghost Jira User"


def test_inbound_assignee_idempotent_reuse(store: Path) -> None:
    """Applying the same inbound assignee twice reuses the one placeholder."""
    for _ in range(2):
        air._ensure_inbound_assignee_identity(
            {"accountId": "acct-inbound-2", "displayName": "Ghost"}, repo_root=str(store)
        )
    ids = [
        t
        for t in rebar.list_tickets(ticket_type="identity", repo_root=str(store))
        if {"provider": "jira", "external_id": "acct-inbound-2"} in t.get("mappings", [])
    ]
    assert len(ids) == 1


def test_inbound_assignee_without_account_id_does_not_fail(store: Path) -> None:
    """A record with no accountId falls back (name-only) without raising or minting."""
    before = len(rebar.list_tickets(ticket_type="identity", repo_root=str(store)))
    air._ensure_inbound_assignee_identity({"displayName": "No Account"}, repo_root=str(store))
    air._ensure_inbound_assignee_identity("just a string", repo_root=str(store))
    after = len(rebar.list_tickets(ticket_type="identity", repo_root=str(store)))
    assert after == before  # nothing minted, no exception
