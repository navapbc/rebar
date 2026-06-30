"""End-to-end rendering of the attestations map in `show` (story ed26, epic
dark-acme-lumen): the kind-keyed map is rendered with the HMAC hex stripped from every kind,
and omitted entirely when the ticket carries no attestations.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import signing


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SIGNING_KEY", "test-key-ed26")
    rebar.init_repo(repo_root=str(repo))
    return repo


def test_show_renders_attestations_map_without_hex(store: Path) -> None:
    tid = rebar.create_ticket("task", "render", repo_root=str(store))
    signing.sign_manifest(tid, ["plan-review: PASS", "m"], kind="plan-review", repo_root=str(store))
    signing.sign_manifest(
        tid, ["completion-verifier: PASS"], kind="completion-verifier", repo_root=str(store)
    )
    shown = rebar.show_ticket(tid, repo_root=str(store))
    assert set(shown["attestations"]) == {"plan-review", "completion-verifier"}
    for kind, rec in shown["attestations"].items():
        assert "signature" not in rec, f"raw HMAC hex leaked for kind {kind}"
        assert rec["manifest"][0].startswith(kind)
        assert rec.get("kind") == kind
    # The legacy back-compat mirror is also hex-stripped in public output.
    assert "signature" not in shown["signature"]


def test_show_omits_attestations_when_none(store: Path) -> None:
    tid = rebar.create_ticket("task", "no attestations", repo_root=str(store))
    shown = rebar.show_ticket(tid, repo_root=str(store))
    assert "attestations" not in shown
