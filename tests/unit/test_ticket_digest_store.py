"""Unit tests for the TICKET_DIGEST sidecar (epic only-crave-art, story 2d0f):
persist + read + freshness of a ticket's Cupid digest.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar.llm.overlap import digest_sidecar as ds
from rebar.reducer._version import _NON_REPLAY_KNOWN_TYPES, is_unknown_newer_type

_DIGEST = {
    "problem_keywords": ["overlap", "duplication"],
    "component_or_area": "plan-review gate",
    "key_entities": ["review_plan", "overlap_verdict"],
    "propositions": ["plan review sees one ticket", "no store-wide overlap detection"],
}
_ACTIVE_MODEL = "test-active-model"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    rebar.init_repo(repo_root=str(r))
    # Pin the "active model" the freshness check compares against, deterministically.
    monkeypatch.setattr(ds, "_active_model", lambda repo_root: _ACTIVE_MODEL)
    return str(r)


def _emit(repo: str, tid: str, digest=None, model: str = _ACTIVE_MODEL) -> None:
    assert ds.emit(digest or dict(_DIGEST), tid, model=model, repo_root=repo) is True


def test_roundtrip(repo: str) -> None:
    tid = rebar.create_ticket("task", "Overlap detector", repo_root=repo)
    _emit(repo, tid)
    payload = ds.latest_ticket_digest(tid, repo_root=repo)
    assert payload is not None
    assert payload["schema"] == "ticket_digest_v1"
    assert payload["digest"] == _DIGEST
    assert payload["model"] == _ACTIVE_MODEL


def test_freshness_all_triggers(repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    tid = rebar.create_ticket("task", "Overlap detector", repo_root=repo)
    _emit(repo, tid)
    assert ds.freshness(tid, repo_root=repo) == "present-fresh"

    # (1) content-hash mismatch: edit the description.
    rebar.edit_ticket(tid, description="a totally different body now", repo_root=repo)
    assert ds.freshness(tid, repo_root=repo) == "present-stale"

    # (2) model mismatch: a digest written under a different model reads stale.
    tid2 = rebar.create_ticket("task", "Another", repo_root=repo)
    _emit(repo, tid2, model="some-old-model")
    assert ds.freshness(tid2, repo_root=repo) == "present-stale"

    # (3) hash-version mismatch: bump the module constant after write.
    tid3 = rebar.create_ticket("task", "Third", repo_root=repo)
    _emit(repo, tid3)
    assert ds.freshness(tid3, repo_root=repo) == "present-fresh"
    monkeypatch.setattr(ds, "DIGEST_HASH_VERSION", ds.DIGEST_HASH_VERSION + 1)
    assert ds.freshness(tid3, repo_root=repo) == "present-stale"


def test_absent(repo: str) -> None:
    tid = rebar.create_ticket("task", "No digest yet", repo_root=repo)
    assert ds.freshness(tid, repo_root=repo) == "absent"
    assert ds.latest_ticket_digest(tid, repo_root=repo) is None


def test_hash_write_read_parity(repo: str) -> None:
    # The write path and the freshness-check path use the SAME _normalize_text, so the
    # content hash written equals the hash recomputed for unchanged state.
    tid = rebar.create_ticket("task", "Parity", repo_root=repo)
    state = rebar.show_ticket(tid, repo_root=repo)
    _emit(repo, tid)
    payload = ds.latest_ticket_digest(tid, repo_root=repo)
    assert payload is not None
    assert payload["content_hash"] == ds.content_hash(state)
    assert ds.freshness(tid, repo_root=repo) == "present-fresh"
    # An edit to a hashed field flips it stale (the normalizer covers title/desc/comments).
    rebar.comment(tid, "a new comment changes the normalized text", repo_root=repo)
    assert ds.freshness(tid, repo_root=repo) == "present-stale"


def test_fail_closed(repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    tid = rebar.create_ticket("task", "Fail closed", repo_root=repo)
    _emit(repo, tid)

    # An unreadable current state → NOT current (fail-closed), never falsely fresh.
    def _boom(*a, **k):
        raise OSError("cannot read current state")

    from rebar import _reads

    monkeypatch.setattr(_reads, "show_ticket", _boom)
    # Pass state=None so freshness must read the (now-broken) current state.
    assert ds.freshness(tid, state=None, repo_root=repo) == "present-stale"


def test_latest_wins(repo: str) -> None:
    tid = rebar.create_ticket("task", "Latest wins", repo_root=repo)
    _emit(repo, tid, digest=dict(_DIGEST, component_or_area="first"))
    _emit(repo, tid, digest=dict(_DIGEST, component_or_area="second"))
    payload = ds.latest_ticket_digest(tid, repo_root=repo)
    assert payload is not None
    assert payload["digest"]["component_or_area"] == "second"


def test_prune_keep1(repo: str) -> None:
    tid = rebar.create_ticket("task", "Prune", repo_root=repo)
    _emit(repo, tid)
    _emit(repo, tid)
    _emit(repo, tid)
    from rebar._commands._seam import tracker_dir

    ticket_dir = Path(tracker_dir(repo)) / tid
    digests = list(ticket_dir.glob("*-TICKET_DIGEST.json"))
    assert len(digests) == 1, f"expected exactly 1 retained digest, got {len(digests)}"


def test_older_binary_ignores(repo: str) -> None:
    # TICKET_DIGEST is a recognized non-replay type, so a binary that predates it
    # preserves-and-ignores it (no fsck "newer than me" WARN), and the reducer skips it.
    assert "TICKET_DIGEST" in _NON_REPLAY_KNOWN_TYPES
    assert is_unknown_newer_type("TICKET_DIGEST") is False
    tid = rebar.create_ticket("task", "Reducer ignores digest", repo_root=repo)
    _emit(repo, tid)
    # The ticket still reduces/reads cleanly with a TICKET_DIGEST event present.
    state = rebar.show_ticket(tid, repo_root=repo)
    assert state["ticket_id"]
    assert "digest_freshness" not in state  # sidecar never enters the shared state shape


def test_cmd_show_renders_freshness(repo: str) -> None:
    tid = rebar.create_ticket("task", "Show render", repo_root=repo)
    _emit(repo, tid)
    import contextlib
    import io

    from rebar._commands._seam import tracker_dir
    from rebar._engine_support.reads_cli import _cmd_show

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _cmd_show([tid], str(tracker_dir(repo)))
    out = buf.getvalue()
    assert rc == 0
    assert '"digest_freshness"' in out
    assert "present-fresh" in out


def test_library_shape_unchanged(repo: str) -> None:
    # The library show/list/search returns still share one shape with a digest present.
    tid = rebar.create_ticket("task", "Shape", repo_root=repo)
    rebar.comment(tid, "searchable body token zzq", repo_root=repo)
    _emit(repo, tid)
    show = rebar.show_ticket(tid, repo_root=repo)
    lst = next(t for t in rebar.list_tickets(repo_root=repo) if t["ticket_id"] == tid)
    srch = next(t for t in rebar.search("zzq", repo_root=repo) if t["ticket_id"] == tid)
    assert set(show) == set(lst) == set(srch)
    assert "digest_freshness" not in show
