"""A throttled read spawns no git process; an unthrottled read still checks, then fetches.

``ensure_fresh`` (``rebar._engine_support.reads``) is the shared read-freshness policy behind
every library, CLI and MCP read. Inside the 60 s marker window it must return before any git
spawn: the audit index alone reaches it 1 + 4N times per request (bug
synonymous-destined-cricket). Outside the window it must still verify the tickets branch
before it writes the marker and fetches.

Spawns are counted at ``subprocess.Popen`` for the duration of one call, attributed to the
calling frame, against a real two-repository store.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import time
import traceback
import types
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

import rebar
from rebar import config as cfg
from rebar._engine_support import reads as ticket_reads

pytestmark = pytest.mark.unit

BASE_ID = "aaaa-bbbb-cccc-dddd"
NEW_ID = "1111-2222-3333-4444"
_READS_FILE = os.path.realpath(ticket_reads.__file__)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
        env=subprocess_env(),
    ).stdout.strip()


def _init_repo(path: Path, branch: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True, env=subprocess_env())
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")


def _commit_ticket(repo: Path, tid: str) -> None:
    tdir = repo / tid
    tdir.mkdir(parents=True)
    ts = time.time_ns()
    ev_uuid = str(uuid.uuid4())
    body = {
        "author": "t",
        "data": {
            "alias": f"alias-{tid[:4]}",
            "description": "",
            "id": tid,
            "parent_id": "",
            "priority": 2,
            "tags": [],
            "ticket_type": "task",
            "title": "T",
        },
        "env_id": str(uuid.uuid4()),
        "event_type": "CREATE",
        "timestamp": ts,
        "uuid": ev_uuid,
    }
    (tdir / f"{ts}-{ev_uuid}-CREATE.json").write_text(json.dumps(body), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", f"CREATE {tid}")


def _build_store(tmp_path: Path, tracker_branch: str) -> types.SimpleNamespace:
    """A code repo whose ``.tickets-tracker`` sits at BASE while ``origin`` holds NEW."""
    repo_root = tmp_path / "proj"
    _init_repo(repo_root, "main")
    origin = tmp_path / "origin"
    _init_repo(origin, "tickets")
    _commit_ticket(origin, BASE_ID)
    tracker = repo_root / ".tickets-tracker"
    _init_repo(tracker, tracker_branch)
    _git(tracker, "remote", "add", "origin", str(origin))
    _git(tracker, "fetch", "-q", "origin", "tickets")
    _git(tracker, "reset", "-q", "--hard", "origin/tickets")
    _commit_ticket(origin, NEW_ID)  # remote-only until a fetch adopts it
    marker = (
        "/tmp/.ticket-sync-" + (hashlib.md5(os.path.realpath(tracker).encode()).hexdigest()[:12])
    )
    return types.SimpleNamespace(repo_root=repo_root, tracker=tracker, marker=Path(marker))


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[types.SimpleNamespace]:
    for name in ("REBAR_SYNC_PULL", "REBAR_TRACKER_DIR", "REBAR_ROOT", "REBAR_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    s = _build_store(tmp_path, "tickets")
    monkeypatch.setenv("REBAR_ROOT", str(s.repo_root))
    cfg.reset_config_cache()
    yield s
    with contextlib.suppress(FileNotFoundError):
        s.marker.unlink()
    cfg.reset_config_cache()


class _GitSpawns:
    """Record ``(argv, from_ensure_fresh)`` for each git process started via Popen."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _GitSpawns:
        real_init = subprocess.Popen.__init__
        calls = self.calls

        def spy(popen, args, *a, **kw):
            argv = [str(x) for x in args] if not isinstance(args, (str, bytes)) else [str(args)]
            if argv and os.path.basename(argv[0]) == "git":
                fresh = any(
                    f.name == "ensure_fresh" and os.path.realpath(f.filename) == _READS_FILE
                    for f in traceback.extract_stack()
                )
                calls.append((argv, fresh))
            return real_init(popen, args, *a, **kw)

        monkeypatch.setattr(subprocess.Popen, "__init__", spy)
        return self

    @property
    def from_ensure_fresh(self) -> list[list[str]]:
        return [argv for argv, fresh in self.calls if fresh]

    def first_index(self, *words: str) -> int:
        for i, (argv, _) in enumerate(self.calls):
            if all(w in argv for w in words):
                return i
        return -1


def _throttle(s: types.SimpleNamespace) -> None:
    s.marker.write_text(str(int(time.time())))


def _unthrottle(s: types.SimpleNamespace) -> None:
    with contextlib.suppress(FileNotFoundError):
        s.marker.unlink()


# ── throttled: no git at all ─────────────────────────────────────────────────
def test_throttled_ensure_fresh_spawns_no_git(store, monkeypatch) -> None:
    _throttle(store)
    spawns = _GitSpawns().install(monkeypatch)
    ticket_reads.ensure_fresh(str(store.tracker))
    assert spawns.calls == []


def test_throttled_ensure_fresh_without_rebar_root_spawns_no_git(store, monkeypatch) -> None:
    """With ``REBAR_ROOT`` unset the config root is discovered with ``git rev-parse
    --show-toplevel``; a throttled read must not reach that discovery either."""
    monkeypatch.delenv("REBAR_ROOT")
    monkeypatch.chdir(store.repo_root)
    cfg.reset_config_cache()
    _throttle(store)
    spawns = _GitSpawns().install(monkeypatch)
    ticket_reads.ensure_fresh(str(store.tracker))
    assert spawns.calls == []


@pytest.mark.parametrize(
    "read",
    [
        pytest.param(lambda: rebar.show_ticket(BASE_ID), id="show_ticket"),
        pytest.param(lambda: rebar.list_tickets(), id="list_tickets"),
    ],
)
def test_throttled_library_read_spawns_no_freshness_git(store, monkeypatch, read) -> None:
    _throttle(store)
    spawns = _GitSpawns().install(monkeypatch)
    read()
    assert spawns.from_ensure_fresh == []
    assert not (store.tracker / NEW_ID).exists()  # no fetch inside the window


# ── unthrottled: the branch check, then the fetch ────────────────────────────
def test_unthrottled_read_verifies_branch_before_fetching(store, monkeypatch) -> None:
    _unthrottle(store)
    spawns = _GitSpawns().install(monkeypatch)
    ticket_reads.ensure_fresh(str(store.tracker))
    verify_at = spawns.first_index("rev-parse", "--verify", "tickets")
    fetch_at = spawns.first_index("fetch")
    assert verify_at >= 0, "branch-existence check did not run"
    assert fetch_at > verify_at, "fetch did not follow the branch check"
    assert store.marker.exists()
    assert (store.tracker / NEW_ID).is_dir()  # remote commit adopted


def test_force_fetches_inside_the_throttle_window(store, monkeypatch) -> None:
    _throttle(store)
    spawns = _GitSpawns().install(monkeypatch)
    ticket_reads.ensure_fresh(str(store.tracker), force=True)
    assert spawns.first_index("fetch") >= 0
    assert (store.tracker / NEW_ID).is_dir()


def test_missing_tickets_branch_neither_fetches_nor_marks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("REBAR_SYNC_PULL", "REBAR_TRACKER_DIR", "REBAR_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    s = _build_store(tmp_path, "main")  # tracker never had a ``tickets`` branch
    monkeypatch.setenv("REBAR_ROOT", str(s.repo_root))
    cfg.reset_config_cache()
    _unthrottle(s)
    try:
        spawns = _GitSpawns().install(monkeypatch)
        ticket_reads.ensure_fresh(str(s.tracker))
        assert spawns.first_index("rev-parse", "--verify", "tickets") >= 0
        assert spawns.first_index("fetch") == -1
        assert not s.marker.exists()
    finally:
        _unthrottle(s)
        cfg.reset_config_cache()


# ── repo-root discovery is not repeated within one read (carbonous-bipartisan-ammonite) ──
def _toplevel_spawns(spawns: _GitSpawns) -> list[list[str]]:
    return [argv for argv, _ in spawns.calls if "--show-toplevel" in argv]


def test_show_ticket_without_rebar_root_discovers_the_root_at_most_once(store, monkeypatch) -> None:
    monkeypatch.delenv("REBAR_ROOT")
    monkeypatch.chdir(store.repo_root)
    cfg.reset_config_cache()
    _throttle(store)
    spawns = _GitSpawns().install(monkeypatch)

    rebar.show_ticket(BASE_ID)

    assert len(_toplevel_spawns(spawns)) <= 1, _toplevel_spawns(spawns)


def test_a_cwd_change_between_reads_resolves_the_new_root(
    store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REBAR_ROOT")
    other = tmp_path / "other"
    _init_repo(other, "main")
    monkeypatch.chdir(store.repo_root)
    cfg.reset_config_cache()
    assert cfg.repo_root() == store.repo_root.resolve()

    monkeypatch.chdir(other)

    assert cfg.repo_root() == other.resolve()


def test_a_repo_created_inside_a_cached_root_is_resolved(
    store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nested repo created after the first lookup is found, not shadowed by the cache."""
    monkeypatch.delenv("REBAR_ROOT")
    nested = store.repo_root / "sub"
    nested.mkdir()
    monkeypatch.chdir(nested)
    cfg.reset_config_cache()
    assert cfg.repo_root() == store.repo_root.resolve()

    _init_repo(nested, "main")

    assert cfg.repo_root() == nested.resolve()


# ── the CLI read path shares that one lookup (carnivoral-scarlet-chihuahua) ──
def test_cli_show_without_rebar_root_discovers_the_root_at_most_once(
    store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``rebar show`` resolved the root in 4 places (snapshot, store mount, init, tracker dir);
    each must answer from the same per-cwd lookup."""
    from rebar._cli import main as cli_main

    monkeypatch.delenv("REBAR_ROOT")
    monkeypatch.chdir(store.repo_root)
    cfg.reset_config_cache()
    _throttle(store)
    spawns = _GitSpawns().install(monkeypatch)

    rc = cli_main(["show", BASE_ID])

    assert rc == 0, capsys.readouterr().err
    assert len(_toplevel_spawns(spawns)) <= 1, _toplevel_spawns(spawns)


def test_repo_root_or_none_is_none_outside_a_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REBAR_ROOT", raising=False)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    bare = tmp_path / "no-repo"
    bare.mkdir()
    monkeypatch.chdir(bare)
    cfg.reset_config_cache()

    assert cfg.repo_root_or_none() is None
    assert cfg.repo_root_or_none() is None  # a miss is never cached as an answer


def test_repo_root_or_none_finds_a_repo_created_inside_a_cached_root(
    store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REBAR_ROOT")
    nested = store.repo_root / "sub"
    nested.mkdir()
    monkeypatch.chdir(nested)
    cfg.reset_config_cache()
    assert os.path.realpath(cfg.repo_root_or_none() or "") == os.path.realpath(store.repo_root)

    _init_repo(nested, "main")

    assert os.path.realpath(cfg.repo_root_or_none() or "") == os.path.realpath(nested)


def test_repo_root_or_none_follows_a_cwd_change(
    store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REBAR_ROOT")
    other = tmp_path / "other"
    _init_repo(other, "main")
    monkeypatch.chdir(store.repo_root)
    cfg.reset_config_cache()
    assert os.path.realpath(cfg.repo_root_or_none() or "") == os.path.realpath(store.repo_root)

    monkeypatch.chdir(other)

    assert os.path.realpath(cfg.repo_root_or_none() or "") == os.path.realpath(other)


def test_repo_root_or_none_drops_a_moved_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached root whose repository moved away is re-resolved, not answered from the memo."""
    monkeypatch.delenv("REBAR_ROOT", raising=False)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = tmp_path / "repo"
    _init_repo(repo, "main")
    sub = repo / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    cfg.reset_config_cache()
    assert os.path.realpath(cfg.repo_root_or_none() or "") == os.path.realpath(repo)

    (repo / ".git").rename(tmp_path / "moved.git")

    assert cfg.repo_root_or_none() is None


def test_reset_config_cache_empties_the_toplevel_memo(store, monkeypatch) -> None:
    monkeypatch.delenv("REBAR_ROOT")
    monkeypatch.chdir(store.repo_root)
    cfg.reset_config_cache()
    cfg.repo_root_or_none()
    from rebar import _config_resolvers

    assert cfg._TOPLEVEL_BY_CWD is _config_resolvers._TOPLEVEL_BY_CWD
    assert _config_resolvers._TOPLEVEL_BY_CWD

    cfg.reset_config_cache()

    assert not _config_resolvers._TOPLEVEL_BY_CWD
