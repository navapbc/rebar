"""Happy-path oracle for 264f (epic gnu-whale-ichor): the provider-neutral
resolution seam.

The ONLY 264f test the implementation sees. Pins the pure-library provider seam —
`resolve_mapping`, `jira_account_id`, `identity_email` — that the reconciler's
outbound assignee/reporter resolution is built on. The reconciler-integration
behaviour (accountId fast-path, sentinel, reporter REST sub-call, degradation) is
validated separately (held out).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar

ACCOUNT_ID = "557058:0a1b2c3d-jira-account"


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


def _identity(store: Path) -> str:
    return rebar.create_identity(
        "Ada Lovelace",
        "ada@example.com",
        mappings=[{"provider": "jira", "external_id": ACCOUNT_ID}],
        repo_root=str(store),
    )


def test_resolve_mapping_by_opaque_id(store: Path) -> None:
    """resolve_mapping keys on the provider's opaque id (never email)."""
    ident = _identity(store)
    assert rebar.resolve_mapping("jira", ACCOUNT_ID, repo_root=str(store)) == ident
    # a provider/id that no identity maps → None
    assert rebar.resolve_mapping("jira", "no-such-account", repo_root=str(store)) is None
    assert rebar.resolve_mapping("github", ACCOUNT_ID, repo_root=str(store)) is None


def test_jira_account_id_for_local_assignee(store: Path) -> None:
    """jira_account_id resolves a local assignee (by identity id or email) to the
    identity's jira accountId; None otherwise."""
    ident = _identity(store)
    assert rebar.jira_account_id(ident, repo_root=str(store)) == ACCOUNT_ID
    assert rebar.jira_account_id("ADA@EXAMPLE.COM", repo_root=str(store)) == ACCOUNT_ID
    assert rebar.jira_account_id("nobody@nowhere.test", repo_root=str(store)) is None


def test_identity_email_for_local_assignee(store: Path) -> None:
    """identity_email returns the matched identity's email (for /user/search)."""
    ident = _identity(store)
    assert rebar.identity_email(ident, repo_root=str(store)) == "ada@example.com"
    assert rebar.identity_email("nobody@nowhere.test", repo_root=str(store)) is None
