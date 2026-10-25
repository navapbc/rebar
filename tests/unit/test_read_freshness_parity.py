"""Read synchronization behaves identically through CLI, library, and MCP facades.

Each real entry point uses the shared throttled reconvergence helper. A two-repository store
verifies remote visibility, one fetch across facades, supported pull opt-outs, and best-effort
local reads after fetch failure.
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
from rebar.llm.plan_review import generation, relation_snapshot

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
    for name in ("REBAR_SYNC_PULL", "REBAR_SYNC_PULL", "REBAR_TRACKER_DIR", "REBAR_ROOT"):
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
        bound_list_payload=lambda rows, **k: rows,
    )
    _mcp_reads.register_read_tools(m, ctx=ctx)
    return m.tools["list_tickets"]


def _library_ids() -> set[str]:
    return {t["ticket_id"] for t in rebar.list_tickets(full=False)}


def _mcp_ids() -> set[str]:
    return {t["ticket_id"] for t in _mcp_list_tool()()}


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
@pytest.mark.parametrize("optout_env", ["REBAR_SYNC_PULL"])
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


# ── 5. gate-material reads force one fetch past the throttle (36d1) ──────────
EDITED_DESCRIPTION = "Description edited on origin after the review began"


def _commit_edit(repo: Path, tid: str, description: str) -> None:
    """Commit an EDIT event that sets ``tid``'s description."""
    ts = time.time_ns()
    ev_uuid = str(uuid.uuid4())
    body = {
        "author": "t",
        "data": {"fields": {"description": description}},
        "env_id": str(uuid.uuid4()),
        "event_type": "EDIT",
        "timestamp": ts,
        "uuid": ev_uuid,
    }
    (repo / tid / f"{ts}-{ev_uuid}-EDIT.json").write_text(json.dumps(body), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", f"EDIT {tid}")


def _stamp_fresh_marker(tracker: Path) -> None:
    """Make the shared throttle think a fetch happened just now."""
    with open(_marker_path(tracker), "w") as fh:
        fh.write(str(int(time.time())))


def _origin_head(store: types.SimpleNamespace) -> str:
    return _git(store.origin, "rev-parse", "HEAD").stdout.strip()


def test_forced_head_read_fetches_inside_the_throttle_window(store, spy) -> None:
    """Inside the 60 s window the default read keeps the stale head and does not fetch;
    the forced read fetches once and returns origin's head."""
    _stamp_fresh_marker(store.tracker)

    # ignore_untracked, as both gate callers pass: reconverge leaves an untracked lock file
    # in this fixture tracker, which the strict status read would otherwise report as dirt.
    stale = relation_snapshot.tracker_head_sha(store.tracker, ignore_untracked=True)
    assert spy.fetches == 0
    assert stale != _origin_head(store), "the default read must stay throttled"

    forced = relation_snapshot.tracker_head_sha(
        store.tracker, ignore_untracked=True, force_fresh=True
    )
    assert spy.fetches == 1
    assert forced == _origin_head(store)


def test_forced_head_read_still_honors_the_pull_optout(
    store, spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    cfg.reset_config_cache()
    _stamp_fresh_marker(store.tracker)

    head = relation_snapshot.tracker_head_sha(store.tracker, force_fresh=True)

    assert spy.fetches == 0
    assert head != _origin_head(store)


def test_forced_head_read_swallows_a_failing_fetch(store, monkeypatch: pytest.MonkeyPatch) -> None:
    """Origin unreachable: the forced read still returns the local head (fail-open)."""
    failing = _FetchSpy(_store_sync.run_git, fail=True)
    monkeypatch.setattr(_store_sync, "run_git", failing)
    _stamp_fresh_marker(store.tracker)

    head = relation_snapshot.tracker_head_sha(store.tracker, force_fresh=True)

    assert failing.fetches == 1, "the forced read did not even try to fetch"
    assert head == _git(store.tracker, "rev-parse", "HEAD").stdout.strip()


def test_forced_snapshot_pins_the_description_edited_on_origin(store, spy) -> None:
    _commit_edit(store.origin, BASE_ID, EDITED_DESCRIPTION)
    _stamp_fresh_marker(store.tracker)

    default = relation_snapshot.collect_plan_relation_snapshot(
        BASE_ID, repo_root=store.repo_root, ignore_untracked=True
    )
    forced = relation_snapshot.collect_plan_relation_snapshot(
        BASE_ID, repo_root=store.repo_root, ignore_untracked=True, force_fresh=True
    )

    assert default.subject_state["description"] == ""
    assert forced.subject_state["description"] == EDITED_DESCRIPTION
    assert forced.ticket_store_revision == _origin_head(store)


def test_sign_manifest_refuses_a_description_changed_inside_the_window(
    store, spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review began on the lagging clone; origin has since changed the description.
    With a fresh marker the signing path must still see it and refuse."""
    from rebar import signing

    def never_signs(*_a, **_k):
        raise AssertionError("signing must not be reached for a superseded plan")

    monkeypatch.setattr(signing, "_sign_manifest_under_lock", never_signs)
    _stamp_fresh_marker(store.tracker)
    initial = generation.collect(BASE_ID, repo_root=store.repo_root, ignore_untracked=True)
    _commit_edit(store.origin, BASE_ID, EDITED_DESCRIPTION)

    with pytest.raises(generation.PlanReviewGenerationChanged):
        generation.sign_manifest(BASE_ID, ["m"], initial, repo_root=store.repo_root)

    assert spy.fetches == 1


def test_sign_manifest_fetches_once_per_attempt_and_never_under_the_lock(
    store, spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worked example: the first attempt hits a lock-time mismatch and retries, so the call
    fetches twice (once per attempt, before the lock) and never while the lock is held."""
    from rebar import signing

    initial = generation.collect(
        BASE_ID, repo_root=store.repo_root, ignore_untracked=True, force_fresh=True
    )
    spy.fetches = 0
    fetched_under_lock: list[int] = []
    attempts: list[int] = []

    def locked_signer(_tid, _manifest, *, under_lock_check, **_kw):
        before = spy.fetches
        under_lock_check()
        fetched_under_lock.append(spy.fetches - before)
        attempts.append(1)
        if len(attempts) == 1:
            raise generation._UnderLockMismatch
        return {"signed": True}

    monkeypatch.setattr(signing, "_sign_manifest_under_lock", locked_signer)

    result = generation.sign_manifest(BASE_ID, ["m"], initial, repo_root=store.repo_root)

    assert result == {"signed": True}
    assert spy.fetches == 2, "one forced fetch per attempt"
    assert fetched_under_lock == [0, 0], "a fetch ran while the lock was held"


def test_review_start_forces_a_fresh_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    from rebar.llm.config import LLMConfig
    from rebar.llm.plan_review import _run_plan_review

    seen: dict = {}

    def record_and_stop(*_a, **kwargs):
        seen.update(kwargs)
        raise relation_snapshot.PlanRelationSnapshotError("missing-target")

    monkeypatch.setattr(relation_snapshot, "collect_plan_relation_snapshot", record_and_stop)

    _run_plan_review(
        "1111-2222-3333-4444",
        cfg=LLMConfig(),
        runner=None,
        sign=True,
        emit_sidecar=False,
        advisory_cap=None,
        repo_root=None,
    )

    assert seen.get("force_fresh") is True
