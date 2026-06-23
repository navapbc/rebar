"""End-to-end coverage for configurable ``tracker.dir`` / ``tracker.branch`` (epic
7c02): a fresh ``init`` honors both, a custom branch survives a write+SYNC round-trip
(push → clone → mount), defaults reproduce today's behavior, and ``fsck`` WARNs when
the configured branch/dir no longer matches what is mounted.

Runnable under ``pytest -m "not integration"``: every "remote" is a LOCAL bare repo
(absolute URL), so there is no network. Marked ``unit`` to match test_e4_init.py.
"""

from __future__ import annotations

import io
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

import rebar
from rebar import config as cfg
from rebar._commands import fsck

pytestmark = pytest.mark.unit


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _make_repo(path: Path, *, config_toml: str = "", origin: Path | None = None) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True)
    if origin is not None:
        subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=path, check=True)
    if config_toml:
        (path / "pyproject.toml").write_text(config_toml, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "base"], cwd=path, check=True)
    return path


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.delenv("TICKETS_TRACKER_DIR", raising=False)
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "always")
    cfg.reset_config_cache()


# ── regression: defaults reproduce today's behavior ──────────────────────────
def test_defaults_dir_and_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path / "repo")
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    cfg.reset_config_cache()
    rebar.init_repo(repo_root=str(repo))
    assert (repo / ".tickets-tracker").is_dir()
    assert _git(repo / ".tickets-tracker", "symbolic-ref", "--short", "HEAD") == "tickets"


# ── custom dir + branch: init creates/mounts/gitignores + write+sync round-trip ──
def test_custom_dir_and_branch_init_and_sync_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toml = '[tool.rebar]\ntracker.dir = "store"\ntracker.branch = "rebar-tickets"\n'
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)

    # Repo A: init at the custom dir + branch, create a ticket (auto-pushes).
    repo_a = _make_repo(tmp_path / "a", config_toml=toml, origin=remote)
    monkeypatch.setenv("REBAR_ROOT", str(repo_a))
    cfg.reset_config_cache()
    rebar.init_repo(repo_root=str(repo_a))

    store = repo_a / "store"
    assert store.is_dir() and not (repo_a / ".tickets-tracker").exists()
    assert _git(store, "symbolic-ref", "--short", "HEAD") == "rebar-tickets"
    # gitignored via the host repo's exclude:
    exclude = (repo_a / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "store" in exclude.split()

    tid = rebar.create_ticket("task", "cross-clone ticket", repo_root=str(repo_a))
    assert tid

    # The auto-push landed on the CUSTOM remote branch (not the default 'tickets').
    refs = _git(remote, "for-each-ref", "--format=%(refname)")
    assert "refs/heads/rebar-tickets" in refs
    assert "refs/heads/tickets" not in refs

    # Repo B: a fresh clone of the SAME remote + config; init mounts the remote branch
    # and the ticket is visible — a true write(A)+sync(B) round-trip.
    repo_b = tmp_path / "b"
    subprocess.run(["git", "clone", "-q", str(remote), str(repo_b)], check=True)
    (repo_b / "pyproject.toml").write_text(toml, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_b), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo_b), "config", "user.name", "T"], check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo_b))
    cfg.reset_config_cache()
    rebar.init_repo(repo_root=str(repo_b))
    assert (repo_b / "store").is_dir()
    assert _git(repo_b / "store", "symbolic-ref", "--short", "HEAD") == "rebar-tickets"
    titles = [t["title"] for t in rebar.list_tickets(repo_root=str(repo_b))]
    assert "cross-clone ticket" in titles


def _run_fsck(repo: Path) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = fsck.fsck_cli([], repo_root=str(repo), no_mutate=True)
    return rc, out.getvalue(), err.getvalue()


# ── fsck WARNs when the configured branch no longer matches the mounted branch ──
def test_fsck_warns_on_branch_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path / "repo", config_toml='[tool.rebar]\ntracker.branch = "branch-a"\n')
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    cfg.reset_config_cache()
    rebar.init_repo(repo_root=str(repo))
    assert _git(repo / ".tickets-tracker", "symbolic-ref", "--short", "HEAD") == "branch-a"

    # Change the configured branch AFTER init (the store is NOT auto-migrated).
    (repo / "pyproject.toml").write_text(
        '[tool.rebar]\ntracker.branch = "branch-b"\n', encoding="utf-8"
    )
    cfg.reset_config_cache()
    rc, out, err = _run_fsck(repo)
    combined = out + err
    assert "WARN" in combined
    assert "branch-b" in combined and "branch-a" in combined
    assert "does not match the mounted" in combined


# ── fsck hints when the configured dir is absent but a default store exists ──
def test_fsck_hints_on_dir_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path / "repo")  # default config → .tickets-tracker
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    cfg.reset_config_cache()
    rebar.init_repo(repo_root=str(repo))

    # Point tracker.dir at a NEW (non-existent) dir; the default store still sits there.
    (repo / "pyproject.toml").write_text(
        '[tool.rebar]\ntracker.dir = "new-store"\n', encoding="utf-8"
    )
    cfg.reset_config_cache()
    rc, out, err = _run_fsck(repo)
    combined = out + err
    assert rc == 1
    assert "new-store" in combined
    assert ".tickets-tracker" in combined  # hints the un-migrated default store
    assert "was changed without migrating" in combined  # the hint-specific fragment
