"""Regression test for `enforce_since` grandfathering (story 8d91 / dedicated-married-ptarmigan).

Complements the held-out oracle (``test_identity_ac7_bff8_heldout.py``, which drives the
boundary through the ``--since`` FLAG) by covering three gaps that motivated the
authenticated-identity rollout (epic pristine-horrible-whapuku):

1. The boundary is driven through the ``identity.enforce_since`` config key via its
   ``REBAR_IDENTITY_ENFORCE_SINCE`` env override, AND enforcement is turned on through the
   ``REBAR_IDENTITY_REQUIRE_AUTHENTICATED`` env override — i.e. the config/env path the
   real merge-gate uses, not the ``--since`` / ``--require-authenticated`` flags.
2. A single MIXED store: one pre-boundary unsigned event is grandfathered AND one
   post-boundary unsigned event is enforced in the SAME run, asserting the per-event
   ``grandfathered`` flag in the ``--format json`` report while the run still exits non-zero.
3. The STRICT-ancestor boundary semantics: an in-scope event whose introducing commit *is*
   the boundary commit is ENFORCED (``git merge-base --is-ancestor X X`` exits 0, so a commit
   is its own ancestor) — only STRICT ancestors of the boundary are grandfathered.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands._seam import tracker_dir


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "dev@example.com"),
        ("git", "config", "user.name", "Dev"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=r, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(r))
    rebar.init_repo(repo_root=str(r))
    return r


def _tracker_head(repo: Path) -> str:
    """HEAD of the `tickets` tracker branch — a valid `enforce_since` boundary ref."""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(tracker_dir(str(repo))),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _gate_via_env(repo: Path, *args: str, enforce_since: str) -> subprocess.CompletedProcess:
    """Run the verify-identity gate with enforcement + boundary driven through the ENV/config
    path (``REBAR_IDENTITY_REQUIRE_AUTHENTICATED`` / ``REBAR_IDENTITY_ENFORCE_SINCE``) — NOT the
    ``--require-authenticated`` / ``--since`` flags. ``args`` carries only scope/output flags."""
    env = {
        **os.environ,
        "REBAR_ROOT": str(repo),
        "REBAR_IDENTITY_REQUIRE_AUTHENTICATED": "1",
        "REBAR_IDENTITY_ENFORCE_SINCE": enforce_since,
    }
    return subprocess.run(
        ["rebar", "verify-identity", *args], cwd=repo, env=env, capture_output=True, text=True
    )


def _entries_for(report: list[dict], ticket_id: str) -> list[dict]:
    return [e for e in report if e.get("ticket_id") == ticket_id]


def test_mixed_store_pre_grandfathered_post_enforced_via_env(repo: Path) -> None:
    """One store, one run, boundary via ENV: the pre-boundary unsigned event is grandfathered
    (``grandfathered: true``) and the post-boundary unsigned event is enforced
    (``grandfathered: false``); the run exits non-zero because the post-boundary event is
    enforced-and-unverified."""
    pre = rebar.create_ticket("task", "pre-boundary unsigned", repo_root=str(repo))
    # A gate-EXEMPT identity advances the branch to the boundary commit without adding an
    # enforceable in-scope event there, so only `pre` (strict ancestor) and `post` are in scope.
    rebar.create_identity("Cutover Marker", "cut@example.com", repo_root=str(repo))
    boundary = _tracker_head(repo)
    post = rebar.create_ticket("task", "post-boundary unsigned", repo_root=str(repo))

    res = _gate_via_env(repo, "--all", "--format", "json", enforce_since=boundary)
    assert res.returncode != 0, res.stdout + res.stderr  # post-boundary enforced → fail

    report = json.loads(res.stdout)
    pre_entries = _entries_for(report, pre)
    post_entries = _entries_for(report, post)
    assert pre_entries, "pre-boundary event missing from report"
    assert all(e["grandfathered"] for e in pre_entries), "pre-boundary must be grandfathered"
    assert post_entries, "post-boundary event missing from report"
    assert all(not e["grandfathered"] for e in post_entries), "post-boundary must be enforced"


def test_all_pre_boundary_grandfathered_passes_via_env(repo: Path) -> None:
    """When every in-scope unsigned event is a strict ancestor of the boundary, the gate PASSES
    (exit 0) even under enforcement — the whole pre-signing history is grandfathered."""
    rebar.create_ticket("task", "only pre-boundary unsigned", repo_root=str(repo))
    rebar.create_identity("Cutover Marker", "cut@example.com", repo_root=str(repo))
    boundary = _tracker_head(repo)

    res = _gate_via_env(repo, "--all", enforce_since=boundary)
    assert res.returncode == 0, res.stdout + res.stderr


def test_event_at_boundary_commit_is_enforced_not_grandfathered(repo: Path) -> None:
    """STRICT-ancestor semantics: an in-scope event whose introducing commit IS the boundary is
    ENFORCED, not grandfathered (a commit is its own ancestor, so
    ``git merge-base --is-ancestor <boundary> <boundary>`` exits 0). Guards against an
    off-by-one that would grandfather the boundary commit itself."""
    rebar.create_ticket("task", "strict-ancestor unsigned", repo_root=str(repo))
    at_boundary = rebar.create_ticket("task", "at-boundary unsigned", repo_root=str(repo))
    boundary = _tracker_head(repo)  # the commit that introduced `at_boundary`

    res = _gate_via_env(repo, "--all", "--format", "json", enforce_since=boundary)
    assert res.returncode != 0, res.stdout + res.stderr

    at_entries = _entries_for(json.loads(res.stdout), at_boundary)
    assert at_entries, "at-boundary event missing from report"
    assert all(not e["grandfathered"] for e in at_entries), "event AT the boundary must be enforced"
