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


def test_default_assignee_resolves_when_the_tracker_lives_outside_the_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relocated tracker still reads ticket.default_assignee from the repository configuration,
    not the tracker's parent directory."""
    monkeypatch.delenv("REBAR_DEFAULT_ASSIGNEE", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    _set_default(repo, "configured@example.com")

    # The store lives OUTSIDE the repo — its parent holds no rebar.toml.
    external = tmp_path / "elsewhere" / "store"
    external.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(external))
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))

    # Precondition: the relocation is real, and its parent genuinely lacks the config.
    assert not (external.parent / "rebar.toml").exists()

    tid = rebar.create_ticket("task", "relocated-store claim", repo_root=str(repo))
    rebar.claim(tid, repo_root=str(repo))

    assert _assignee(tid, repo) == "configured@example.com", (
        "a relocated store must resolve ticket.default_assignee from the REPO, not from the "
        "tracker's parent — otherwise every claim on the deployed MCP server records no owner"
    )


def test_plan_review_gate_still_applies_when_the_tracker_lives_outside_the_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moving the tracker must not disable the repository's plan-review claim gate. Its
    configuration comes from the repo root."""
    monkeypatch.delenv("REBAR_DEFAULT_ASSIGNEE", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "rebar.toml").write_text(
        "[verify]\nrequire_plan_review_for_claim = true\n", encoding="utf-8"
    )

    external = tmp_path / "elsewhere" / "store"
    external.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(external))
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    assert not (external.parent / "rebar.toml").exists()

    # A TASK — bugs and session_logs are exempt from this gate, so they cannot show it.
    tid = rebar.create_ticket("task", "relocated-store gate", repo_root=str(repo))

    with pytest.raises(Exception) as exc:  # CommandError surfaces through the seam
        rebar.claim(tid, repo_root=str(repo))
    assert "plan" in str(exc.value).lower() or "review" in str(exc.value).lower(), (
        "the plan-review gate must still BLOCK a claim when the store is relocated; reading "
        f"the flag from the tracker's parent resolves an empty config and silently disables "
        f"the gate. got: {exc.value!r}"
    )
