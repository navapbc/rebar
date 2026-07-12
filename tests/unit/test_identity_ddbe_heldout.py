"""HELD-OUT oracle for ddbe — the implementation MUST NOT see this file.

Validates the parts that separate a real implementation from one that fakes the
happy path: the seven-site type exemption (graph/list/gates/Jira-sync), every
resolver fallback degrading to ``None``, generated-types registration, and the
end-to-end CLI create path. Asserts observable behaviour only.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import get_args

import pytest

import rebar

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


# ---------------------------------------------------------------- registration


def test_identity_is_a_registered_ticket_type() -> None:
    """The generated TicketType Literal includes 'identity' (codegen ran)."""
    from rebar.types import TicketType

    assert "identity" in get_args(TicketType)


def test_identity_excluded_from_jira_sync() -> None:
    """identity is in the reconciler's never-sync set (mirrors session_log).

    The reconciler lives under ``src/rebar/_engine/rebar_reconciler`` which is
    shadowed by the ``rebar._engine`` module, so load config.py by file path
    (same approach as the session_log-exclusion test).
    """
    import importlib.util

    config_path = (
        Path(rebar.__file__).resolve().parent / "_engine" / "rebar_reconciler" / "config.py"
    )
    spec = importlib.util.spec_from_file_location("rebar_reconciler_config_ident", config_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "identity" in mod.EXCLUDED_SYNC_TYPES


# ------------------------------------------------------------------ exemptions


def test_identity_absent_from_default_list_ready_next_batch(store: Path) -> None:
    """An identity is never scheduled as dispatchable work."""
    epic = rebar.create_ticket("epic", "Some epic", repo_root=str(store))
    task = rebar.create_ticket("task", "A task", parent=epic, repo_root=str(store))
    ident = rebar.create_identity("Ada", "ada@example.com", repo_root=str(store))

    listed = {t["ticket_id"] for t in rebar.list_tickets(repo_root=str(store))}
    assert ident not in listed
    assert task in listed  # sanity: normal work still lists

    ready_ids = {t["ticket_id"] for t in rebar.ready(repo_root=str(store))}
    assert ident not in ready_ids

    nb = rebar.next_batch(epic, repo_root=str(store))
    batch_ids = {t.get("id") or t.get("ticket_id") for t in nb.get("batch", [])}
    assert ident not in batch_ids


def test_identity_passes_per_ticket_gates(store: Path) -> None:
    """clarity/AC/quality gates are exempt (always pass) for an identity."""
    ident = rebar.create_identity("Ada", "ada@example.com", repo_root=str(store))
    assert rebar.check_ac(ident, repo_root=str(store))["passed"] is True
    assert rebar.quality_check(ident, repo_root=str(store))["passed"] is True
    assert rebar.clarity_check(ident, repo_root=str(store))["passed"] is True


# ------------------------------------------------------------ resolver fallbacks


def test_resolve_returns_none_when_git_email_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=repo, check=True, capture_output=True)
    # deliberately NO user.email configured, isolate global config
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty_gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "empty_gitconfig"))
    subprocess.run(
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "x",
            "GIT_AUTHOR_EMAIL": "x@x",
            "GIT_COMMITTER_NAME": "x",
            "GIT_COMMITTER_EMAIL": "x@x",
            "PATH": __import__("os").environ["PATH"],
        },
    )
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    rebar.create_identity("Ada", "ada@example.com", repo_root=str(repo))
    assert rebar.resolve_current_identity(repo_root=str(repo)) is None


def test_resolve_returns_none_when_git_missing(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing git-email subprocess degrades to None, never propagates.

    Create the identity FIRST (real git), then make ONLY the resolver's
    ``git config user.email`` lookup blow up — the store write/read paths keep
    working so we exercise the resolver's degradation, not a broken fixture.
    """
    rebar.create_identity("Ada", "ada@example.com", repo_root=str(store))

    real_run = subprocess.run
    real_check_output = subprocess.check_output

    def _fail_git_email(cmd, *a, **k):
        parts = cmd if isinstance(cmd, (list, tuple)) else str(cmd).split()
        if "config" in parts and any("user.email" in str(p) for p in parts):
            raise OSError("git not found")
        return real_run(cmd, *a, **k)

    def _fail_git_email_co(cmd, *a, **k):
        parts = cmd if isinstance(cmd, (list, tuple)) else str(cmd).split()
        if "config" in parts and any("user.email" in str(p) for p in parts):
            raise OSError("git not found")
        return real_check_output(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "run", _fail_git_email)
    monkeypatch.setattr(subprocess, "check_output", _fail_git_email_co, raising=False)
    assert rebar.resolve_current_identity(repo_root=str(store)) is None


def test_resolve_returns_none_on_zero_match(store: Path) -> None:
    rebar.create_identity("Ada", "nobody@nowhere.test", repo_root=str(store))
    # git email is dev@example.com; the only identity has a different email.
    assert rebar.resolve_current_identity(repo_root=str(store)) is None


def test_resolve_returns_none_on_ambiguous_match(store: Path) -> None:
    rebar.create_identity("Ada", GIT_EMAIL, repo_root=str(store))
    rebar.create_identity("Ada2", GIT_EMAIL, repo_root=str(store))
    assert rebar.resolve_current_identity(repo_root=str(store)) is None


def test_resolve_dangling_pointer_falls_through_then_none(store: Path) -> None:
    """A pointer to a non-existent id falls through to git-email match (here: none)."""
    ptr = store / ".rebar" / "current_identity"
    ptr.parent.mkdir(parents=True, exist_ok=True)
    ptr.write_text(json.dumps({"identity_id": "dead-beef-dead-beef"}))
    rebar.create_identity("Ada", "nomatch@nowhere.test", repo_root=str(store))
    assert rebar.resolve_current_identity(repo_root=str(store)) is None


# ------------------------------------------------------------------------- E2E


def test_cli_identity_create_and_show_e2e(store: Path) -> None:
    """rebar identity create → rebar show --output llm renders all fields."""
    env = {**__import__("os").environ, "REBAR_ROOT": str(store)}
    create = subprocess.run(
        [
            "rebar",
            "identity",
            "create",
            "--name",
            "Ada Lovelace",
            "--email",
            "ada@example.com",
            "--mapping",
            "jira:acc-999",
            "--key",
            KEY,
        ],
        cwd=store,
        env=env,
        capture_output=True,
        text=True,
    )
    assert create.returncode == 0, create.stderr
    ident_id = create.stdout.strip().split()[-1]

    show = subprocess.run(
        ["rebar", "show", ident_id, "--output", "llm"],
        cwd=store,
        env=env,
        capture_output=True,
        text=True,
    )
    assert show.returncode == 0, show.stderr
    out = show.stdout
    assert "ada@example.com" in out  # email
    assert "acc-999" in out  # mapping external_id
    assert "Ada Lovelace" in out  # name
    assert KEY in out  # keys — all four AC2 fields must render via --output llm


def test_cli_identity_use_sets_pointer_e2e(store: Path) -> None:
    env = {**__import__("os").environ, "REBAR_ROOT": str(store)}
    ident = rebar.create_identity("Ada", "ada@example.com", repo_root=str(store))
    use = subprocess.run(
        ["rebar", "identity", "use", ident],
        cwd=store,
        env=env,
        capture_output=True,
        text=True,
    )
    assert use.returncode == 0, use.stderr
    ptr = store / ".rebar" / "current_identity"
    assert ptr.is_file()
    assert json.loads(ptr.read_text())["identity_id"] == ident
