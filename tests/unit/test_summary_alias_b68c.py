"""Summary exposes the exact alias so agents can retain human-friendly identifiers."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import rebar
from rebar._commands import composer

pytestmark = pytest.mark.unit


@pytest.fixture
def rebar_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _summary_cli(repo: Path, *ticket_ids: str) -> list[dict]:
    completed = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "summary", *ticket_ids, "--output", "json"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_resolved_summary_returns_alias_and_preserves_caller_token(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical = "a123-4567-89ab-cdef"
    monkeypatch.setattr(composer, "_new_ticket_id", lambda: canonical)
    created = rebar.create_ticket(
        "bug", "Alias-bearing summary", repo_root=str(rebar_repo), return_alias=True
    )
    alias = created["alias"]
    prefix = canonical[:4]

    library = rebar.summary(canonical, alias, prefix, repo_root=str(rebar_repo))
    cli = _summary_cli(rebar_repo, canonical, alias, prefix)

    for result in (library, cli):
        assert [item["ticket_id"] for item in result] == [canonical, alias, prefix]
        assert [item["alias"] for item in result] == [alias, alias, alias]


def test_ambiguous_prefix_fails_closed_with_null_alias(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = iter(("b68c-1111-2222-3333", "b68c-aaaa-bbbb-cccc"))
    monkeypatch.setattr(composer, "_new_ticket_id", lambda: next(ids))
    rebar.create_ticket("bug", "First collision", repo_root=str(rebar_repo))
    rebar.create_ticket("bug", "Second collision", repo_root=str(rebar_repo))

    library = rebar.summary("b68c", repo_root=str(rebar_repo))
    cli = _summary_cli(rebar_repo, "b68c")

    for result in (library, cli):
        assert result == [
            {
                "ticket_id": "b68c",
                "alias": None,
                "status": "unknown",
                "title": None,
                "blocking_summary": None,
            }
        ]
