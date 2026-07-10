"""End-to-end harness for infra/scripts/reviewbot-ensure-tickets.sh (bug
desirous-judicial-hogget / d220).

A fresh ``git clone --single-branch --branch tickets`` of the shared tickets branch — the
review-bot's persistent artifact store — is NOT a usable rebar store: it has no repo-local
git identity and lacks the git-ignored ``.env-id`` marker, so every write fails "ticket
system not initialized" (composer.py) and ``emit_code_review_artifact`` swallows it, making
artifact emission a silent no-op on every fresh clone.

This harness is fully hermetic (all local git; no network, no Docker, no real GitHub):

  RED         — a store write into a fresh single-branch clone FAILS today;
  GREEN       — after the ensure script runs, the SAME write SUCCEEDS + is durably committed;
  idempotence — running the ensure step twice is a no-op and the write still succeeds;
  AC#3        — ``emit_code_review_artifact`` emits the greppable ``ARTIFACT_EMIT_ERROR``
                marker when a write into a non-initialized dir fails.

Proving command:
    .venv/bin/pytest tests/unit/test_reviewbot_ensure_tickets.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import rebar
from rebar._errors import RebarError

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
ENSURE_SCRIPT = _REPO_ROOT / "infra" / "scripts" / "reviewbot-ensure-tickets.sh"


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _isolated_git_env(home: Path) -> dict[str, str]:
    """Env that gives git NO ambient identity (empty global + system config, empty HOME),
    reproducing the container's bare python:slim image where the fresh clone has no identity.
    Inherits the runner's PYTHONPATH pin so a subprocess ``import rebar`` resolves this src."""
    home.mkdir(exist_ok=True)
    return {
        **os.environ,
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),  # absent ⇒ empty
        "GIT_CONFIG_SYSTEM": os.devnull,
    }


@pytest.fixture
def origin_with_tickets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A host repo with an initialized rebar store + a seed ticket, so its ``tickets`` branch
    carries real store content — the source we single-branch-clone from (mirrors the
    container cloning the shared tickets branch)."""
    # Strip ambient git identity for the whole test so the fresh clone is identity-less and
    # the store's "not initialized" gate is the ONLY thing standing between us and a write.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("REBAR_SIGNING_KEY", "test-signing-key-d220")
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.delenv("REBAR_ROOT", raising=False)

    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    # Repo-local identity ONLY on origin (so its own commits work); the clone stays bare.
    _git("config", "user.email", "seed@e.com", cwd=origin)
    _git("config", "user.name", "seed", cwd=origin)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=origin)
    rebar.init_repo(repo_root=str(origin))
    rebar.create_ticket("task", "seed ticket", repo_root=str(origin))
    return origin


def _fresh_clone(origin: Path, dest: Path, home: Path) -> Path:
    """A fresh single-branch clone of the ``tickets`` branch with no identity + no .env-id —
    exactly the container's fresh-clone state (git clone --single-branch --branch tickets)."""
    subprocess.run(
        ["git", "clone", "-q", "--single-branch", "--branch", "tickets", str(origin), str(dest)],
        check=True,
        capture_output=True,
        text=True,
        env=_isolated_git_env(home),
    )
    return dest


def _run_ensure(clone: Path, home: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the REAL ensure shell script against ``clone`` with the test interpreter (so the
    subprocess ``import rebar`` resolves this worktree's src via the inherited PYTHONPATH)."""
    env = _isolated_git_env(home)
    env["REVIEWBOT_TICKETS_DIR"] = str(clone)
    env["REVIEWBOT_PYTHON"] = sys.executable
    return subprocess.run(["sh", str(ENSURE_SCRIPT)], env=env, capture_output=True, text=True)


def _write_into(clone: Path) -> str:
    """A store write (code_review artifact) into ``clone`` as the review-bot does — the
    ambient tracker is the clone itself (REBAR_TRACKER_DIR), mirroring docker-compose.yml."""
    os.environ["REBAR_TRACKER_DIR"] = str(clone)
    try:
        return rebar.create_ticket("code_review", "code-review: I1 @ r1", repo_root=str(clone))
    finally:
        os.environ.pop("REBAR_TRACKER_DIR", None)


# ── RED: a fresh single-branch clone is not writable ───────────────────────────────────────
def test_fresh_single_branch_clone_write_fails(origin_with_tickets: Path, tmp_path: Path) -> None:
    """RED state: a fresh clone lacks ``.env-id`` (git-ignored), so a store write fails
    'ticket system not initialized' — reproducing the review-bot's silent emission no-op."""
    clone = _fresh_clone(origin_with_tickets, tmp_path / "clone_red", tmp_path / "home")
    assert not (clone / ".env-id").exists()  # the store marker is git-ignored ⇒ absent
    with pytest.raises(RebarError) as ei:
        _write_into(clone)
    assert "not initialized" in str(ei.value)


# ── GREEN: the ensure script makes it writable + durably committed ─────────────────────────
def test_ensure_makes_clone_writable_and_durable(origin_with_tickets: Path, tmp_path: Path) -> None:
    """After the ensure script runs, the SAME write SUCCEEDS and the event is durably
    committed (git HEAD advances on the clone). Also proves AC#1(a): a repo-local git
    identity was set on the clone."""
    home = tmp_path / "home"
    clone = _fresh_clone(origin_with_tickets, tmp_path / "clone_green", home)

    # No identity on the fresh clone (RED precondition for the AC#1a assertion).
    pre = subprocess.run(
        ["git", "-C", str(clone), "config", "user.email"], capture_output=True, text=True
    )
    assert pre.stdout.strip() == ""

    result = _run_ensure(clone, home)
    assert result.returncode == 0, result.stderr
    assert (clone / ".env-id").exists()  # store marker now present

    # AC#1(a): a repo-local git identity was set (default review-bot values).
    email = subprocess.run(
        ["git", "-C", str(clone), "config", "user.email"], capture_output=True, text=True
    ).stdout.strip()
    assert email == "rebar-review-bot@navateam.com"

    head_before = _git("rev-parse", "HEAD", cwd=clone).stdout.strip()
    tid = _write_into(clone)
    assert tid  # the write SUCCEEDS now (GREEN)
    head_after = _git("rev-parse", "HEAD", cwd=clone).stdout.strip()
    assert head_after != head_before  # the event is durably committed to the store


# ── idempotence: running the ensure step twice is a no-op; the write still succeeds ────────
def test_ensure_is_idempotent(origin_with_tickets: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    clone = _fresh_clone(origin_with_tickets, tmp_path / "clone_idem", home)
    assert _run_ensure(clone, home).returncode == 0
    second = _run_ensure(clone, home)
    assert second.returncode == 0, second.stderr
    # Second run reports the env-id/config units as already converged (no 'changed' for them).
    assert "ensure env-id: ok" in second.stderr
    tid = _write_into(clone)
    assert tid  # still writable after a second ensure


# ── AC#3: the swallowed emission failure emits a greppable marker ──────────────────────────
def test_emission_failure_emits_greppable_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A write into a NON-initialized dir fails inside ``emit_code_review_artifact``; the
    swallow now emits the greppable ``ARTIFACT_EMIT_ERROR`` marker (AC#3) rather than being a
    silent no-op — while still continuing (returns None, never crashes the review)."""
    from rebar.review_bot.voter import emit_code_review_artifact

    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    baddir = tmp_path / "no_store"
    baddir.mkdir()  # exists, but has no rebar store (.env-id) ⇒ writes fail

    decision = {"decision": "PASS", "verdict": {"verdict": "PASS", "blocking": [], "advisory": []}}
    art = emit_code_review_artifact(
        decision,
        change_id="Idead",
        revision="r1",
        commit_message="x",
        diff_text="d",
        repo_root=str(baddir),
    )
    assert art is None  # continue-don't-crash preserved
    captured = capsys.readouterr()
    assert "ARTIFACT_EMIT_ERROR" in captured.err  # greppable journald marker emitted
    assert "Idead" in captured.err  # carries the change id for correlation
