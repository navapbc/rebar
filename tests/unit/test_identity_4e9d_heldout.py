"""HELD-OUT oracle for 4e9d — the implementation MUST NOT see this file.

Validates the parts that separate a real implementation from one that fakes the
happy path: attribution at ALL FOUR local envelope composers (append_event / txn /
delete / compact), replay back-compat (pre-change events reduce identically),
the author_email failure branch, the per-repo memoization cardinality, per-entry
reduced-output surfacing, and top-level reduced-state surfacing. Observable only.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands._seam import tracker_dir

GIT_EMAIL = "dev@example.com"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Keep events UNFOLDED so raw per-event envelopes are inspectable (the unit
    # conftest defaults the compaction horizon to 0, which folds them into a
    # SNAPSHOT immediately). Compaction tests override this back to 0 locally.
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "9" * 18)
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


def _all_events(repo: Path, tid: str) -> list[dict]:
    tdir = Path(tracker_dir(str(repo))) / tid
    out = []
    for name in sorted(os.listdir(tdir)):
        if name.endswith(".json") and not name.startswith("."):
            out.append(json.loads((tdir / name).read_text()))
    return out


def _events(repo: Path, tid: str, etype: str) -> list[dict]:
    return [e for e in _all_events(repo, tid) if e.get("event_type") == etype]


# ---------------------------------------------- all four local composer sites


def test_append_event_composer_stamps_email(store: Path) -> None:
    """create + comment both flow through append_event and carry author_email."""
    tid = rebar.create_ticket("task", "T", repo_root=str(store))
    rebar.comment(tid, "a comment", repo_root=str(store))
    assert _events(store, tid, "CREATE")[0]["author_email"] == GIT_EMAIL
    assert _events(store, tid, "COMMENT")[0]["author_email"] == GIT_EMAIL


def test_txn_composer_stamps_email(store: Path) -> None:
    """claim (txn.py: STATUS + EDIT) and transition (STATUS) carry author_email."""
    tid = rebar.create_ticket("task", "T", repo_root=str(store))
    rebar.claim(tid, assignee="agent", repo_root=str(store))
    rebar.transition(tid, "in_progress", "closed", repo_root=str(store))
    status = _events(store, tid, "STATUS")
    assert status, "expected STATUS events"
    assert all("author_email" in e for e in status)


def test_delete_composer_stamps_email(store: Path) -> None:
    """delete.py's directly-composed events (UNLINK scan) carry author_email."""
    a = rebar.create_ticket("task", "A", repo_root=str(store))
    b = rebar.create_ticket("task", "B", repo_root=str(store))
    rebar.link(a, b, "relates_to", repo_root=str(store))
    # delete is CLI-only (destructive; requires --user-approved).
    env = {**os.environ, "REBAR_ROOT": str(store)}
    res = subprocess.run(
        ["rebar", "delete", b, "--user-approved"],
        cwd=store,
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    # the UNLINK written onto A by the delete scan is composed in delete.py
    unlinks = _events(store, a, "UNLINK")
    assert unlinks, "expected an UNLINK event from the delete scan"
    assert all("author_email" in e for e in unlinks)


def test_compact_snapshot_composer_stamps_email(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """compact.py SNAPSHOT (via _compact_locked) carries author_email."""
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")  # allow folding
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    tid = rebar.create_ticket("task", "T", repo_root=str(store))
    for i in range(4):
        rebar.comment(tid, f"c{i}", repo_root=str(store))
    rebar.compact(tid, repo_root=str(store))
    snaps = _events(store, tid, "SNAPSHOT")
    assert snaps, "expected a SNAPSHOT after compaction"
    assert snaps[-1]["author_email"] == GIT_EMAIL


def test_rebuild_snapshot_composer_stamps_email(store: Path) -> None:
    """compact.py's rebuild_snapshot_from_full_log path also stamps author_email."""
    from rebar._commands import compact as _compact

    tid = rebar.create_ticket("task", "T", repo_root=str(store))
    rebar.comment(tid, "c", repo_root=str(store))
    tracker = str(tracker_dir(str(store)))
    ticket_dir = os.path.join(tracker, tid)
    _compact.rebuild_snapshot_from_full_log(tracker, tid, ticket_dir)
    snaps = _events(store, tid, "SNAPSHOT")
    assert snaps, "expected a rebuilt SNAPSHOT"
    assert snaps[-1]["author_email"] == GIT_EMAIL


# ------------------------------------------------------ identity / reduced state


def test_reduced_state_surfaces_author_id_top_level(store: Path) -> None:
    ident = rebar.create_identity("Dev", GIT_EMAIL, repo_root=str(store))
    rebar.use_identity(ident, repo_root=str(store))
    tid = rebar.create_ticket("task", "T", repo_root=str(store))
    state = rebar.show_ticket(tid, repo_root=str(store))
    assert state["author_email"] == GIT_EMAIL
    assert state["author_id"] == ident


def test_reduced_state_omits_author_id_without_identity(store: Path) -> None:
    tid = rebar.create_ticket("task", "T", repo_root=str(store))
    state = rebar.show_ticket(tid, repo_root=str(store))
    assert "author_id" not in state


def test_per_entry_comment_attribution(store: Path) -> None:
    """A comment sub-record carries author_email (present-only)."""
    ident = rebar.create_identity("Dev", GIT_EMAIL, repo_root=str(store))
    rebar.use_identity(ident, repo_root=str(store))
    tid = rebar.create_ticket("task", "T", repo_root=str(store))
    rebar.comment(tid, "hello", repo_root=str(store))
    state = rebar.show_ticket(tid, repo_root=str(store))
    comments = state.get("comments", [])
    assert comments and comments[-1]["author_email"] == GIT_EMAIL
    assert comments[-1]["author_id"] == ident


# --------------------------------------------------------------- back-compat


def test_backcompat_pre_change_event_reduces_identically(store: Path) -> None:
    """A synthetic event stream WITHOUT author_email/author_id reduces with neither
    key (top-level or per-entry) and never raises."""
    from rebar.reducer import reduce_ticket

    tracker = Path(tracker_dir(str(store)))
    tid = "0000-1111-2222-3333"
    tdir = tracker / tid
    tdir.mkdir(parents=True)
    create = {
        "timestamp": "100-0-abc",
        "uuid": "u-create",
        "event_type": "CREATE",
        "env_id": "envx",
        "author": "Legacy User",  # note: NO author_email / author_id
        "data": {"ticket_type": "task", "title": "Old", "id": tid, "priority": 2},
    }
    (tdir / "100-u-create-CREATE.json").write_text(json.dumps(create))
    comment = {
        "timestamp": "200-0-abc",
        "uuid": "u-comment",
        "event_type": "COMMENT",
        "env_id": "envx",
        "author": "Legacy User",
        "data": {"text": "old comment"},
    }
    (tdir / "200-u-comment-COMMENT.json").write_text(json.dumps(comment))

    state = reduce_ticket(str(tdir))
    assert state is not None
    assert state["status"] == "open"
    assert state["author"] == "Legacy User"
    # NEITHER new key appears anywhere (byte-identical to pre-change behaviour).
    assert "author_email" not in state
    assert "author_id" not in state
    for c in state.get("comments", []):
        assert "author_email" not in c
        assert "author_id" not in c


# ------------------------------------------------------- author_email failure


def test_author_email_returns_empty_on_git_failure(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._commands import _seam

    def _boom(cmd, *a, **k):
        raise OSError("git not found")

    monkeypatch.setattr(_seam.subprocess, "run", _boom)
    assert _seam.author_email() == ""


# --------------------------------------------------------------- cache cardinality


def test_attribution_resolved_at_most_once_per_process(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Across N>=3 sequential writes, the git-email lookup AND the identity scan each
    run at most once (per-repo memoization)."""
    from rebar._commands import _seam
    from rebar._commands import identity as _identity

    if hasattr(_seam, "_reset_attribution_cache"):
        _seam._reset_attribution_cache()

    email_calls = {"n": 0}
    ident_calls = {"n": 0}

    real_email = _seam.author_email

    def counting_email(*a, **k):
        email_calls["n"] += 1
        return real_email(*a, **k)

    real_resolve = _identity.resolve_current_identity

    def counting_resolve(*a, **k):
        ident_calls["n"] += 1
        return real_resolve(*a, **k)

    monkeypatch.setattr(_seam, "author_email", counting_email)
    monkeypatch.setattr(_identity, "resolve_current_identity", counting_resolve)

    tid = rebar.create_ticket("task", "T", repo_root=str(store))
    for i in range(3):
        rebar.comment(tid, f"c{i}", repo_root=str(store))

    assert email_calls["n"] <= 1, f"git-email resolved {email_calls['n']} times"
    assert ident_calls["n"] <= 1, f"identity resolved {ident_calls['n']} times"
