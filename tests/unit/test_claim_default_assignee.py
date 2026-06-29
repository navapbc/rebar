"""claim falls back to ticket.default_assignee when no assignee is given (story c36c).

- Omitted `--assignee` (CLI) / `assignee=None` (lib) → use the configured default.
- Explicit `--assignee X` always wins; explicit `--assignee ""` clears (no fallback).
- The fallback is resolved at the TOP of claim_compute, so a parent-cascade claim
  inherits the default too (advisory f7ca28).
- The CLI output (JSON + report suffix) reflects the resolved default (advisory f9ea30).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import claim as claim_mod

pytestmark = pytest.mark.unit


@pytest.fixture
def rebar_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("REBAR_DEFAULT_ASSIGNEE", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _set_default(repo: Path, value: str) -> None:
    (repo / "rebar.toml").write_text(f'[ticket]\ndefault_assignee = "{value}"\n', encoding="utf-8")


def _assignee(tid: str, repo: Path):
    return rebar.show_ticket(tid, repo_root=str(repo)).get("assignee")


def test_claim_uses_default_when_omitted(rebar_repo: Path) -> None:
    _set_default(rebar_repo, "dana@example.com")
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    rebar.claim(tid, repo_root=str(rebar_repo))  # no assignee
    assert _assignee(tid, rebar_repo) == "dana@example.com"


def test_explicit_assignee_wins_over_default(rebar_repo: Path) -> None:
    _set_default(rebar_repo, "dana@example.com")
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="alice", repo_root=str(rebar_repo))
    assert _assignee(tid, rebar_repo) == "alice"


def test_explicit_empty_clears_no_fallback(rebar_repo: Path) -> None:
    """An explicit empty assignee means 'no assignee' — it must NOT trigger the
    default fallback (only an omitted assignee does)."""
    _set_default(rebar_repo, "dana@example.com")
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    rebar.claim(tid, assignee="", repo_root=str(rebar_repo))
    assert _assignee(tid, rebar_repo) in (None, "")


def test_no_default_leaves_unassigned(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    rebar.claim(tid, repo_root=str(rebar_repo))
    assert _assignee(tid, rebar_repo) in (None, "")


def test_env_override_supplies_default(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_DEFAULT_ASSIGNEE", "env@example.com")
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    rebar.claim(tid, repo_root=str(rebar_repo))
    assert _assignee(tid, rebar_repo) == "env@example.com"


def test_cascade_applies_default_to_open_parent(rebar_repo: Path) -> None:
    """The default must be resolved before the parent-cascade so a claimed child's
    open parent inherits the same default assignee (advisory f7ca28)."""
    _set_default(rebar_repo, "dana@example.com")
    parent = rebar.create_ticket("epic", "parent", repo_root=str(rebar_repo))
    child = rebar.create_ticket("task", "child", parent=parent, repo_root=str(rebar_repo))
    rebar.claim(child, repo_root=str(rebar_repo))  # no assignee
    assert _assignee(child, rebar_repo) == "dana@example.com"
    assert _assignee(parent, rebar_repo) == "dana@example.com"  # cascaded default


def test_cli_output_reflects_default(rebar_repo: Path, capsys: pytest.CaptureFixture) -> None:
    """`claim` CLI JSON output must show the resolved default, not null (advisory f9ea30)."""
    _set_default(rebar_repo, "dana@example.com")
    tid = rebar.create_ticket("task", "t", repo_root=str(rebar_repo))
    capsys.readouterr()  # drain
    rc = claim_mod.claim_cli(["--output", "json", tid], repo_root=str(rebar_repo))
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(out)["assignee"] == "dana@example.com"
