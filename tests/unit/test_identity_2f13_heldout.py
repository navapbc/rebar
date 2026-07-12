"""HELD-OUT oracle for 2f13 — the implementation MUST NOT see this file.

Validates the parts the happy path cannot: the in-place upgrade of a placeholder
(one identity, not two, even with a changed display name), the provider-neutral
stable-id keying (same external_id → one; different → two), and that an EXISTING
real identity mapping that accountId is reused rather than shadowed by a ghost.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "dev@example.com"),
        ("git", "config", "user.name", "Dev"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _identities(store: Path) -> list[dict]:
    return rebar.list_tickets(ticket_type="identity", repo_root=str(store))


def _mapping_count(store: Path, provider: str, external_id: str) -> int:
    return sum(
        1
        for t in _identities(store)
        if {"provider": provider, "external_id": external_id} in t.get("mappings", [])
    )


def test_upgrade_in_place_no_duplicate(store: Path) -> None:
    """A second call with a better display_name upgrades the placeholder title IN
    PLACE — still exactly one identity for that accountId."""
    a = rebar.ensure_identity_for("jira", "acct-1", "acct-1", repo_root=str(store))
    b = rebar.ensure_identity_for("jira", "acct-1", "Ada Lovelace", repo_root=str(store))
    assert a == b
    assert _mapping_count(store, "jira", "acct-1") == 1
    assert rebar.show_ticket(a, repo_root=str(store))["title"] == "Ada Lovelace"


def test_provider_neutral_stable_id_keying(store: Path) -> None:
    """Keyed on the opaque external_id: same id → one identity across display names;
    a different external_id → a distinct identity."""
    a = rebar.ensure_identity_for("jira", "acct-A", "Name One", repo_root=str(store))
    a2 = rebar.ensure_identity_for("jira", "acct-A", "Name Two", repo_root=str(store))
    b = rebar.ensure_identity_for("jira", "acct-B", "Someone Else", repo_root=str(store))
    assert a == a2
    assert b != a
    assert _mapping_count(store, "jira", "acct-A") == 1
    assert _mapping_count(store, "jira", "acct-B") == 1


def test_reuses_existing_real_identity(store: Path) -> None:
    """When a REAL identity already maps the accountId, ensure_identity_for returns
    it (does not mint a shadow ghost), and does not mark the real one a placeholder."""
    real = rebar.create_identity(
        "Real Ada",
        "ada@example.com",
        mappings=[{"provider": "jira", "external_id": "acct-real"}],
        repo_root=str(store),
    )
    got = rebar.ensure_identity_for("jira", "acct-real", "Ada From Jira", repo_root=str(store))
    assert got == real
    assert _mapping_count(store, "jira", "acct-real") == 1
    # the pre-existing real identity is not a placeholder
    assert rebar.is_placeholder(real, repo_root=str(store)) is False


def test_placeholder_marked_real_is_not_a_ghost(store: Path) -> None:
    """After a placeholder is enriched (its placeholder tag removed), is_placeholder
    reports False and the same accountId still resolves to it."""
    a = rebar.ensure_identity_for("jira", "acct-x", "Ghost", repo_root=str(store))
    assert rebar.is_placeholder(a, repo_root=str(store)) is True
    rebar.untag(a, "placeholder", repo_root=str(store))
    assert rebar.is_placeholder(a, repo_root=str(store)) is False
    again = rebar.ensure_identity_for("jira", "acct-x", "Ghost", repo_root=str(store))
    assert again == a
    assert _mapping_count(store, "jira", "acct-x") == 1
