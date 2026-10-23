"""Happy-path identity entity checks.

create_identity writes one identity CREATE with email, mappings, and keys;
show_ticket exposes it. use_identity stores the pointer; resolution reads it
or falls back to a case-insensitive Git email match."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar

# git identity of the store fixture below — the default-match target.
GIT_EMAIL = "dev@example.com"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKeyMaterialForTestingOnly01"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", GIT_EMAIL),
        ("git", "config", "user.name", "Dev Example"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def test_create_identity_roundtrip(store: Path) -> None:
    """A create mints an identity carrying name/email/mappings/keys; show renders them."""
    tid = rebar.create_identity(
        "Ada Lovelace",
        "ada@example.com",
        mappings=[{"provider": "jira", "external_id": "acc-557058:abc"}],
        keys=[KEY],
        repo_root=str(store),
    )
    assert isinstance(tid, str) and tid

    t = rebar.show_ticket(tid, repo_root=str(store))
    assert t["ticket_type"] == "identity"
    assert t["title"] == "Ada Lovelace"
    assert t["email"] == "ada@example.com"
    assert t["mappings"] == [{"provider": "jira", "external_id": "acc-557058:abc"}]
    assert t["keys"] == [KEY]


def test_resolve_current_identity_pointer_hit(store: Path) -> None:
    """use_identity writes the pointer; resolve reads it back."""
    tid = rebar.create_identity("Grace Hopper", "grace@example.com", repo_root=str(store))
    rebar.use_identity(tid, repo_root=str(store))
    assert rebar.resolve_current_identity(repo_root=str(store)) == tid


def test_resolve_current_identity_git_email_default(store: Path) -> None:
    """With no pointer, resolve defaults to a case-insensitive git-email match."""
    # email intentionally cased differently from the git config value.
    tid = rebar.create_identity("Dev", GIT_EMAIL.upper(), repo_root=str(store))
    assert rebar.resolve_current_identity(repo_root=str(store)) == tid
