"""Read-freshness parity across the three facades (ticket 799f).

CLI, library, and MCP reads all funnel through the ONE shared
``rebar._engine_support.reads.ensure_fresh`` reconvergence helper (a best-effort,
throttled ``git fetch origin tickets`` + reconverge gated by a shared
``/tmp/.ticket-sync-<md5>`` marker). Existing tests only prove the
dispatcher-passthrough (by patching ``ensure_fresh``); nothing proves the actual
*behavioral* contract holds identically through each facade's real entry point.

This module drives the REAL facade entries against a two-repo git store (a local
``origin`` whose ``origin/tickets`` carries a NEW commit) with a COUNTED fetch seam
(monkeypatching ``rebar._store.sync.run_git`` — the git helper reconverge calls —
so real fetch *attempts* are counted while everything else still runs for real),
and asserts on OBSERVABLE behavior only (fetch count, visible ticket ids):

1. remote visibility through each facade (parametrized over all three);
2. the shared throttle: reads across the three facades fetch exactly once total;
3. the three opt-outs (``REBAR_SYNC_PULL=off``, ``REBAR_NO_SYNC=1``, ``--no-pull`` /
   ``no_sync=True``) suppress the fetch while local replay still returns the store;
4. a failing fetch leaves the facade returning consistent local state, no leak.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import time
import types
import uuid
from pathlib import Path

import pytest

import rebar
from rebar import config as cfg
from rebar._engine_support import reads as ticket_reads
from rebar._engine_support import reads_cli
from rebar._store import sync as _store_sync

pytestmark = pytest.mark.unit

BASE_ID = "aaaa-bbbb-cccc-dddd"
NEW_ID = "1111-2222-3333-4444"


# ── git store construction ───────────────────────────────────────────────────
def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )


def _init_repo(path: Path, branch: str = "tickets") -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")


def _commit_ticket(repo: Path, tid: str, alias: str, title: str) -> str:
    """Write a valid CREATE event for ``tid`` and commit it; return the commit SHA."""
    tdir = repo / tid
    tdir.mkdir(parents=True, exist_ok=True)
    ts = time.time_ns()
    ev_uuid = str(uuid.uuid4())
    body = {
        "author": "t",
        "data": {
            "alias": alias,
            "description": "",
            "id": tid,
            "parent_id": "",
            "priority": 2,
            "tags": [],
            "ticket_type": "task",
            "title": title,
        },
        "env_id": str(uuid.uuid4()),
        "event_type": "CREATE",
        "timestamp": ts,
        "uuid": ev_uuid,
    }
    (tdir / f"{ts}-{ev_uuid}-CREATE.json").write_text(json.dumps(body), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", f"CREATE {tid}")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A repo whose ``.tickets-tracker`` has an ``origin`` remote; ``origin/tickets``
    holds a NEW commit (``NEW_ID``) the tracker has not yet adopted (tracker sits at
    the shared ``BASE_ID`` base)."""
    for name in ("REBAR_SYNC_PULL", "REBAR_NO_SYNC", "REBAR_TRACKER_DIR", "REBAR_ROOT"):
        monkeypatch.delenv(name, raising=False)

    repo_root = tmp_path / "proj"
    _init_repo(repo_root, branch="main")  # repo_root just needs to be a git work tree

    origin = tmp_path / "origin"
    _init_repo(origin, branch="tickets")
    _commit_ticket(origin, BASE_ID, "base-alias", "Base ticket present in both")

    tracker = repo_root / ".tickets-tracker"
    _init_repo(tracker, branch="tickets")
    _git(tracker, "remote", "add", "origin", str(origin))
    _git(tracker, "fetch", "-q", "origin", "tickets")
    _git(tracker, "reset", "--hard", "origin/tickets")  # tracker == BASE now

    # The NEW commit lands on origin AFTER the tracker synced — invisible until fetch.
    _commit_ticket(origin, NEW_ID, "new-alias", "New remote ticket")

    monkeypatch.setenv("REBAR_ROOT", str(repo_root))
    cfg.reset_config_cache()
    return types.SimpleNamespace(repo_root=repo_root, tracker=tracker, origin=origin)


# ── counted fetch seam ───────────────────────────────────────────────────────
class _FetchSpy:
    """Wraps the real ``run_git`` reconverge calls, counting ``fetch`` attempts.

    Installed over ``rebar._store.sync.run_git`` (the sole git helper reconverge
    uses). Non-fetch git ops delegate to the real helper so reconverge still runs
    for real; when ``fail`` is set, a fetch attempt raises (the failure seam)."""

    def __init__(self, real, *, fail: bool = False) -> None:
        self._real = real
        self.fail = fail
        self.fetches = 0

    def __call__(self, cwd, *args, **kwargs):
        if args and args[0] == "fetch":
            self.fetches += 1
            if self.fail:
                raise RuntimeError("simulated fetch failure")
        return self._real(cwd, *args, **kwargs)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _FetchSpy:
    s = _FetchSpy(_store_sync.run_git)
    monkeypatch.setattr(_store_sync, "run_git", s)
    return s


# ── throttle-marker helpers ──────────────────────────────────────────────────
def _marker_path(tracker: Path) -> str:
    md5 = hashlib.md5(os.path.realpath(str(tracker)).encode()).hexdigest()[:12]
    return f"/tmp/.ticket-sync-{md5}"


def _expire_marker(tracker: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.remove(_marker_path(tracker))


# ── the three real facade read entries (id-set from each) ────────────────────
def _mcp_list_tool():
    from rebar import _mcp_reads

    class _FakeMcp:
        def __init__(self) -> None:
            self.tools: dict = {}

        def tool(self, **_kw):
            def deco(fn):
                self.tools[fn.__name__] = fn
                return fn

            return deco

    m = _FakeMcp()
    ctx = types.SimpleNamespace(
        readonly=False,
        allow_jira_sync=False,
        cap_workflow_payload=lambda *a, **k: None,
        MODE_CAPS={},
        Mode=None,
    )
    _mcp_reads.register_read_tools(m, ctx=ctx)
    return m.tools["list_tickets"]


def _library_ids() -> set[str]:
    return {t["ticket_id"] for t in rebar.list_tickets(full=False)}


def _mcp_ids() -> set[str]:
    return {t.ticket_id for t in _mcp_list_tool()()}


def _cli_ids(extra: tuple[str, ...] = ()) -> set[str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = reads_cli.main(["list", *extra])
    assert rc == 0, f"cli list exited {rc}"
    return {t["ticket_id"] for t in json.loads(buf.getvalue())}


FACADES = {"library": _library_ids, "cli": _cli_ids, "mcp": _mcp_ids}


# ── 1. remote visibility across all three facades ────────────────────────────
@pytest.mark.parametrize("facade", list(FACADES))
def test_facade_read_makes_remote_update_visible(store, spy, facade: str) -> None:
    """Each facade's read triggers exactly one fetch+reconverge that adopts the
    remote-only NEW ticket, making it visible in that facade's own returned data."""
    _expire_marker(store.tracker)
    ids = FACADES[facade]()
    assert spy.fetches == 1, f"{facade} did not fetch exactly once"
    assert NEW_ID in ids, f"{facade} did not surface the remote update"
    assert BASE_ID in ids


# ── 2. shared throttle: reads across the three facades fetch exactly once ─────
def test_shared_throttle_no_double_fetch(store, spy) -> None:
    """The throttle marker is SHARED, so the first facade read fetches and the two
    that follow (within the 60s window) skip — one fetch total across all three."""
    _expire_marker(store.tracker)
    all_ids: set[str] = set()
    for read in FACADES.values():
        all_ids |= read()
    assert spy.fetches == 1, "throttle marker not shared across facades (double fetch)"
    assert NEW_ID in all_ids  # the single fetch adopted the update for everyone


# ── 3. opt-outs keep the remote invisible; local replay still works ──────────
@pytest.mark.parametrize("facade", list(FACADES))
@pytest.mark.parametrize("optout_env", ["REBAR_SYNC_PULL", "REBAR_NO_SYNC"])
def test_env_optout_suppresses_fetch_but_replays_local(
    store, spy, monkeypatch: pytest.MonkeyPatch, facade: str, optout_env: str
) -> None:
    """With an env opt-out set, no facade fetches (counter stays 0) so the remote
    update stays invisible, yet each facade still replays the local store (BASE)."""
    monkeypatch.setenv(optout_env, "off" if optout_env == "REBAR_SYNC_PULL" else "1")
    cfg.reset_config_cache()
    _expire_marker(store.tracker)
    ids = FACADES[facade]()
    assert spy.fetches == 0, f"{facade} fetched despite {optout_env} opt-out"
    assert NEW_ID not in ids, "remote update visible despite opt-out"
    assert BASE_ID in ids, "local replay broken under opt-out"


def test_param_optout_cli_and_helper_suppress_fetch(store, spy) -> None:
    """The ``--no-pull`` CLI flag and the shared ``ensure_fresh(no_sync=True)`` param
    both suppress the fetch while the local store still reads back (BASE)."""
    _expire_marker(store.tracker)
    cli_ids = _cli_ids(("--no-pull",))
    assert spy.fetches == 0, "cli --no-pull fetched"
    assert NEW_ID not in cli_ids and BASE_ID in cli_ids

    _expire_marker(store.tracker)
    ticket_reads.ensure_fresh(str(store.tracker), no_sync=True)
    assert spy.fetches == 0, "ensure_fresh(no_sync=True) fetched"


# ── 4. fetch failure yields consistent local state, no leak ──────────────────
@pytest.mark.parametrize("facade", list(FACADES))
def test_fetch_failure_returns_consistent_local_state(
    store, monkeypatch: pytest.MonkeyPatch, facade: str
) -> None:
    """When the fetch seam raises, the facade read neither leaks the exception nor
    corrupts state — it returns the pre-fetch local snapshot (BASE, not NEW)."""
    failing = _FetchSpy(_store_sync.run_git, fail=True)
    monkeypatch.setattr(_store_sync, "run_git", failing)
    _expire_marker(store.tracker)
    ids = FACADES[facade]()  # must not raise
    assert failing.fetches >= 1, "fetch was not even attempted"
    assert NEW_ID not in ids, "adopted a remote update the fetch never delivered"
    assert BASE_ID in ids, "local replay broken on fetch failure"
