"""A single central CLI gate mounts the store before dispatch, so no store-touching command
can skip it — including the pure intercepts (bug ad9f, discovered from 80af).

`rebar verify-commit-ticket` is a PURE INTERCEPT dispatched before the arms that call the
centralized ``ensure_initialized``, and it does not self-manage init — yet it resolves ids
against the store. In a fresh linked/cloned worktree with no ``.tickets-tracker`` yet, it died
with "ticket store not found" instead of auto-mounting (attach-to-existing / symlink) the way
``claim``/``list`` do. The fix hoists the MOUNT (``ensure_initialized(init_only=True)``, already
a no-op under ``REBAR_TRACKER_DIR`` override and for a genuine first-time init non-interactively)
to one central gate before both the intercepts and the set-based dispatch.

These pin the CONTRACT (observable ``main()`` behavior), not the wiring:
* a store-touching PURE INTERCEPT (verify-commit-ticket) auto-mounts + resolves in a fresh
  clone whose ``origin`` already carries a ``tickets`` branch — no manual ``rebar init``.
* a store READ arm (list) still auto-mounts there (no regression).
* a NO-STORE command (explain) is NOT force-mounted: it still runs in a greenfield repo with no
  ticket store, non-interactively, WITHOUT triggering the first-time-init consent/refusal.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._cli import _init, main


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def clone_with_origin_tickets(tmp_path, monkeypatch):
    """A bare origin carrying a seeded ``tickets`` branch + a FRESH clone with no local
    ``.tickets-tracker`` yet. Yields (clone_path, seeded_ticket_id)."""
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", cwd=seed)
    _git("config", "user.email", "t@t", cwd=seed)
    _git("config", "user.name", "t", cwd=seed)
    _git("commit", "-q", "--allow-empty", "-m", "root", cwd=seed)
    _git("remote", "add", "origin", str(origin), cwd=seed)
    _git("push", "-q", "origin", "HEAD:main", cwd=seed)

    monkeypatch.setenv("REBAR_ROOT", str(seed))
    rebar.init_repo(repo_root=str(seed))
    tid = rebar.create_ticket("task", "findme via central mount", repo_root=str(seed))
    _git("push", "-q", "origin", "tickets:tickets", cwd=seed / ".tickets-tracker")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    _git("config", "user.email", "t@t", cwd=clone)
    _git("config", "user.name", "t", cwd=clone)
    assert not (clone / ".tickets-tracker").exists()
    return clone, tid


def _use_clone(clone: Path, monkeypatch) -> None:
    monkeypatch.setenv("REBAR_ROOT", str(clone))
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.chdir(clone)
    # Force the non-interactive branch (the parallel-agent environment): the attach-to-existing
    # mount must still happen automatically, but a genuine first-time init must still refuse.
    monkeypatch.setattr(_init, "_is_interactive", lambda: False)


def test_verify_commit_ticket_auto_mounts_in_fresh_clone(
    clone_with_origin_tickets, monkeypatch, capsys
) -> None:
    """THE bug: the pure intercept resolves an id against the store in a fresh clone by
    auto-mounting first, instead of failing 'ticket store not found'."""
    clone, tid = clone_with_origin_tickets
    # Enable the gate so the command actually RESOLVES the id against the store (with the gate
    # off it short-circuits before any store access, which wouldn't exercise the mount).
    (clone / "rebar.toml").write_text("[verify]\nrequire_ticket_for_commit = true\n")
    _use_clone(clone, monkeypatch)

    code = main(["verify-commit-ticket", "--message", f"{tid}: the work"])

    assert code == 0, capsys.readouterr()
    assert (clone / ".tickets-tracker").is_dir(), "store was not mounted"


def test_store_read_still_auto_mounts_in_fresh_clone(
    clone_with_origin_tickets, monkeypatch, capsys
) -> None:
    """Regression guard: a normal store read arm still auto-mounts in the fresh clone."""
    clone, tid = clone_with_origin_tickets
    _use_clone(clone, monkeypatch)

    code = main(["list"])

    assert code == 0
    out = capsys.readouterr().out
    assert tid in out, out


def test_no_store_command_is_not_force_mounted(tmp_path, monkeypatch, capsys) -> None:
    """Guard: a NO-STORE command (explain) must not be force-mounted — it still runs in a
    greenfield repo (no tickets branch anywhere), non-interactively, WITHOUT tripping the
    first-time-init refusal that mounting a store would raise there."""
    repo = tmp_path / "greenfield"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@t", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "root", cwd=repo)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(_init, "_is_interactive", lambda: False)

    # `explain plan` reads the packaged guide; it needs no store and must not be forced to make
    # one. Must NOT raise SystemExit (the first-time-init refusal) and must not mount a store.
    code = main(["explain", "plan"])

    assert code == 0, capsys.readouterr()
    assert not (repo / ".tickets-tracker").exists(), "explain wrongly force-mounted a store"
