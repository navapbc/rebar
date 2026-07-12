"""Happy-path oracle for 2f13 (epic gnu-whale-ichor): inbound ghost/placeholder
identity with idempotent auto-upgrade.

The ONLY 2f13 test the implementation sees. Pins the resolve-or-mint contract on the
happy path: an unmapped inbound user mints a placeholder identity, and re-running for
the SAME (provider, external_id) returns the SAME identity (one, not two). The
in-place upgrade and provider-neutral-key edges are validated separately (held out).
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


def test_mint_placeholder_for_unmapped_inbound_user(store: Path) -> None:
    """An unmapped inbound Jira user mints a placeholder identity storing
    (provider, external_id, display_name)."""
    ident = rebar.ensure_identity_for("jira", "acct-557", "Ada Jira", repo_root=str(store))
    assert isinstance(ident, str) and ident

    t = rebar.show_ticket(ident, repo_root=str(store))
    assert t["ticket_type"] == "identity"
    assert t["title"] == "Ada Jira"
    assert {"provider": "jira", "external_id": "acct-557"} in t["mappings"]
    assert rebar.is_placeholder(ident, repo_root=str(store)) is True


def test_rerun_is_idempotent_one_identity(store: Path) -> None:
    """Re-running for the SAME (provider, external_id) returns the SAME id — one
    identity, not two."""
    a = rebar.ensure_identity_for("jira", "acct-999", "Grace", repo_root=str(store))
    b = rebar.ensure_identity_for("jira", "acct-999", "Grace", repo_root=str(store))
    assert a == b
    matching = [
        t
        for t in _identities(store)
        if {"provider": "jira", "external_id": "acct-999"} in t.get("mappings", [])
    ]
    assert len(matching) == 1
