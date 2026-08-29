"""Library reads must not report an ABSENT ticket store as an EMPTY one.

WHY THIS TEST EXISTS. The in-process read path (`rebar._reads`) resolves the tracker
directory through one chokepoint, `_reads._tracker()`, and for a long time never checked
that the directory existed. One layer down, `reducer/_api.py` wraps its `os.listdir(tracker)`
in `except OSError: return results` — and `FileNotFoundError` IS an `OSError` — so a store
that is not there at all reduced to `[]` with no error at all.

That is not a cosmetic difference. Every sibling surface treats an absent store as an error:
the CLI reads (`_engine_support/reads_cli.py:340,384`) and the library WRITES
(`_store/event_prepare.py:99-102`). Only library reads stayed silent, and the MCP read tools
delegate straight to them (`_mcp_reads.py` -> `rebar.list_tickets`). The consequence was
observed in production: an MCP server deployed with no store answered `list_tickets`,
`search` and `ready` with `[]` while `fsck` correctly reported "ticket system not
initialized". A driving agent is then told "there are no tickets" when the truth is "this
store is missing", which is precisely the fallback-masking that `docs/mcp-reference.md:7`
forbids -- faults surface as errors "instead of silently resolving to a fallback", so "a
driving agent can tell a broken config from a deliberate policy refusal".

The parametrized set is EVERY read entry the guard governs, not a sample. `ready` is the
library spelling of the MCP `ready_tickets` tool (one of the three that answered `[]` on
the storeless production server); `deps` and `next_batch` are the remaining two that
funnel through `_tracker()`. Sampling here is what let the gap reach review twice --
plan review caught the missing `ready`, code review caught `deps`/`next_batch` -- so the
set is now exhaustive by construction: any new read entry added to `_reads.py` that is
not listed here is an untested inheritance of this guard.

THE DISTINCTION THIS PINS, and the reason the last test below is not optional: the fix must
separate "the tracker directory does not exist" (an error) from "the tracker exists and holds
no tickets" (a legitimate empty result). A guard that raises on both would be just as wrong
in the other direction, and would break every caller that reads a freshly-initialized store.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._errors import RebarError

_NOT_INITIALIZED = "not initialized"


def _bare_repo(tmp_path: Path) -> Path:
    """A git repo with NO ticket store — `rebar init` is deliberately never run."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    # Precondition, asserted rather than assumed: the whole test rests on this being absent.
    assert not (repo / ".tickets-tracker").exists()
    return repo


def _initialized_empty_repo(tmp_path: Path) -> Path:
    """An initialized store holding zero tickets — the legitimate `[]` case."""
    repo = tmp_path / "empty"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    rebar.init_repo(repo_root=repo)
    assert (repo / ".tickets-tracker").is_dir()
    return repo


@pytest.mark.parametrize(
    ("name", "call"),
    [
        ("list_tickets", lambda repo: rebar.list_tickets(repo_root=repo)),
        ("search", lambda repo: rebar.search("anything", repo_root=repo)),
        ("ready", lambda repo: rebar.ready(repo_root=repo)),
        ("recent_session_logs", lambda repo: rebar.recent_session_logs(repo_root=repo)),
        ("deps", lambda repo: rebar.deps("abcd-1234-5678-9abc", repo_root=repo)),
        ("next_batch", lambda repo: rebar.next_batch("abcd-1234-5678-9abc", repo_root=repo)),
    ],
)
def test_reads_raise_on_absent_store_instead_of_returning_empty(
    tmp_path: Path, name: str, call
) -> None:
    """A read against a store that does not exist is an ERROR, never an empty list."""
    repo = _bare_repo(tmp_path)

    with pytest.raises(RebarError) as excinfo:
        call(repo)

    assert _NOT_INITIALIZED in str(excinfo.value), (
        f"{name} must name the real fault (uninitialized store), got: {excinfo.value}"
    )


def test_show_ticket_on_absent_store_blames_the_store_not_the_id(tmp_path: Path) -> None:
    """`show_ticket` already raised — but for the WRONG reason.

    Against a missing store it reported "Ticket '<id>' not found", sending the reader off to
    hunt for a ticket when the store itself was never there.
    """
    repo = _bare_repo(tmp_path)

    with pytest.raises(RebarError) as excinfo:
        rebar.show_ticket("abcd-1234-5678-9abc", repo_root=repo)

    assert _NOT_INITIALIZED in str(excinfo.value), (
        f"show_ticket must blame the missing store, not the id, got: {excinfo.value}"
    )


def test_reads_and_writes_agree_that_an_absent_store_is_an_error(tmp_path: Path) -> None:
    """The parity this defect broke: same store, same library, same process.

    The write path has always raised here (`event_prepare._ensure_initialized`). This asserts
    the read path now agrees, so the two surfaces can no longer disagree about whether an
    absent store is a fault.
    """
    repo = _bare_repo(tmp_path)

    with pytest.raises(RebarError) as write_err:
        rebar.create_ticket("task", "probe", repo_root=repo)
    with pytest.raises(RebarError) as read_err:
        rebar.list_tickets(repo_root=repo)

    assert _NOT_INITIALIZED in str(write_err.value)
    assert _NOT_INITIALIZED in str(read_err.value)


def test_initialized_but_empty_store_still_returns_empty(tmp_path: Path) -> None:
    """The no-regression half: an EXISTING store with no tickets is not an error.

    This is what keeps the guard honest. It must key on the tracker's existence, not on the
    result being empty -- otherwise a freshly-initialized store would start raising.
    """
    repo = _initialized_empty_repo(tmp_path)

    assert rebar.list_tickets(repo_root=repo) == []
    assert rebar.search("anything", repo_root=repo) == []
    assert rebar.ready(repo_root=repo) == []


def test_audit_index_renders_empty_rather_than_500ing_without_a_store(tmp_path: Path) -> None:
    """The one caller that legitimately depends on the OLD degrade-to-empty behaviour.

    `rebar.audit.server._audited_tickets` backs a read-only web index and documents itself as
    best-effort. Making library reads raise is right for a driving agent -- it can act on
    "the store is missing" -- but a browser hitting the audit index should see an empty page,
    not a 500. This pins that the guard is scoped to the uninitialized-store case: the index
    degrades, while a genuinely broken store still propagates.

    This test exists because the caller sweep for the parent fix searched `_reads.*` consumers
    and so never saw this call site, which reaches the same code through the PUBLIC
    `rebar.list_tickets`. Code review caught it.
    """
    from rebar.audit.server import _audited_tickets

    repo = _bare_repo(tmp_path)

    assert _audited_tickets(repo_root=str(repo)) == []


def test_audit_index_still_propagates_a_non_store_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OTHER half of the audit guard: only the uninitialized store degrades.

    A guard that swallowed every RebarError would turn a genuinely broken store into a
    blank page — the same silent-empty failure this whole change exists to remove, just
    relocated. Only the `store_uninitialized` code is caught; anything else propagates.
    """
    import rebar
    from rebar.audit import server as audit_server

    def _boom(**_kw: object) -> list[dict]:
        err = rebar.RebarError("rebar list failed (exit 1): store is corrupt")
        err.error_code = "command_failed"
        raise err

    monkeypatch.setattr(audit_server.rebar, "list_tickets", _boom)

    with pytest.raises(rebar.RebarError, match="corrupt"):
        audit_server._audited_tickets(repo_root=str(tmp_path))


# ---------------------------------------------------------------------------
# rapt-dreadable-dromedary (aefe-614a-2631-4117): a tracker directory that
# EXISTS but is not a USABLE store (no `.git` and no store structure, or `.git`
# with an unresolvable HEAD mid-clone) must read as `store_uninitialized`, not
# `[]`. The old guard keyed on `os.path.isdir(tracker)` alone, so a
# present-but-unusable store slid through and every read reduced to an empty list
# — a broken store made indistinguishable from an empty one. These pin the
# widened predicate (`store_usability.store_is_usable`: isdir AND (a live `.git`
# with a resolvable HEAD OR the rebar store structure on disk — the committed
# `.store-compat.json` record or a ticket event dir)) across every read entry
# point, the shared write gate, and the sole `store_uninitialized` consumer (the
# audit index). The store-structure clause keeps a `.git`-less MATERIALIZED
# snapshot readable (the gate-agent read surface) while a genuinely broken store
# still raises.
# ---------------------------------------------------------------------------

_READ_ENTRY_POINTS = [
    ("list_tickets", lambda repo: rebar.list_tickets(repo_root=repo)),
    ("search", lambda repo: rebar.search("anything", repo_root=repo)),
    ("ready", lambda repo: rebar.ready(repo_root=repo)),
    ("recent_session_logs", lambda repo: rebar.recent_session_logs(repo_root=repo)),
    ("deps", lambda repo: rebar.deps("abcd-1234-5678-9abc", repo_root=repo)),
    ("next_batch", lambda repo: rebar.next_batch("abcd-1234-5678-9abc", repo_root=repo)),
]


def _tracker_present_without_git(tmp_path: Path) -> Path:
    """The PRODUCTION shape: a tracker DIRECTORY that exists (holds marker files) but has NO
    `.git` at all. `isdir` is true, so the old guard passed it through and reads returned `[]`.

    The tracker is deliberately nested inside a git-initialised code repo so the guard's
    ordering matters: a naive `git -C tracker rev-parse HEAD` would WALK UP to the enclosing
    repo. The predicate must reject on the absent `.git` BEFORE it ever probes HEAD.
    """
    repo = tmp_path / "brokenrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    tracker = repo / ".tickets-tracker"
    tracker.mkdir()
    (tracker / "reviewbot-ensure-tickets").write_text("marker\n")
    assert tracker.is_dir() and not (tracker / ".git").exists()
    return repo


def _tracker_midclone_unresolvable_head(tmp_path: Path) -> Path:
    """A store MID-CLONE: the tracker `.git` is present but HEAD does not resolve yet (an
    unborn branch, before any objects/refs land). `isdir` and `.git`-presence are both true,
    so only a HEAD-resolvability probe can tell this apart from a finished clone."""
    repo = tmp_path / "midclone"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    tracker = repo / ".tickets-tracker"
    tracker.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=tracker, check=True)
    # Precondition: HEAD is unborn, so `rev-parse --verify HEAD` fails.
    probe = subprocess.run(
        ["git", "-C", str(tracker), "rev-parse", "--verify", "-q", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode != 0, "fixture must present an UNRESOLVABLE HEAD"
    assert (tracker / ".git").exists()
    return repo


def _clone_without_env_id(tmp_path: Path) -> Path:
    """A valid, HEAD-resolvable store that is missing its git-ignored `.env-id` — the exact
    shape right after a fresh clone (events are versioned; `.env-id` is local state that is
    not). Reads MUST succeed here; only WRITES require the `.env-id` provenance stamp. This is
    why the read predicate keys on `.git`/HEAD and deliberately does NOT adopt `.env-id`."""
    repo = _initialized_empty_repo(tmp_path)
    env_id = repo / ".tickets-tracker" / ".env-id"
    if env_id.exists():
        env_id.unlink()
    assert not env_id.exists()
    return repo


_STORE_COMPAT_RECORD = '{"format_version": 1, "required_capabilities": []}'


def _write_create_event(event_dir: Path, tid: str, title: str) -> None:
    """Write a minimal well-formed CREATE event (the shape the reducer materializes a ticket
    from) into *event_dir*, mirroring the store's on-disk layout."""
    event_dir.mkdir(parents=True, exist_ok=True)
    (event_dir / "001-CREATE.json").write_text(
        json.dumps(
            {
                "event_type": "CREATE",
                "ticket_id": tid,
                "timestamp": 1700000000000000000,
                "uuid": f"u-{tid}-0001",
                "env_id": "test",
                "author": "test",
                "data": {
                    "ticket_id": tid,
                    "title": title,
                    "ticket_type": "task",
                    "status": "open",
                    "priority": 2,
                    "parent_id": None,
                },
            }
        )
    )


def _gitless_snapshot(tracker: Path, *, with_event: bool = True) -> Path:
    """A `.git`-LESS tracker carrying the store STRUCTURE — the shape
    `_snapshot.materialize_tickets` produces (a checked-out tickets tree, no `.git`): the
    committed `.store-compat.json` record, and (optionally) a ticket event dir. Returns the
    tracker dir itself."""
    tracker.mkdir(parents=True, exist_ok=True)
    (tracker / ".store-compat.json").write_text(_STORE_COMPAT_RECORD)
    if with_event:
        _write_create_event(tracker / "abcd-1234-5678-9abc", "abcd-1234-5678-9abc", "snap ticket")
    assert not (tracker / ".git").exists()
    return tracker


def _gitless_event_dir_only(tracker: Path) -> Path:
    """A `.git`-LESS tracker with a ticket event dir but NO committed record — a legacy store
    shape; the store-structure clause's event-dir fallback must accept it. Returns the tracker."""
    tracker.mkdir(parents=True, exist_ok=True)
    _write_create_event(tracker / "abcd-1234-5678-9abc", "abcd-1234-5678-9abc", "legacy ticket")
    assert not (tracker / ".git").exists() and not (tracker / ".store-compat.json").exists()
    return tracker


def _gitless_snapshot_repo(tmp_path: Path, *, with_event: bool = True) -> Path:
    """A repo ROOT whose `.tickets-tracker/` is a `.git`-less materialized snapshot (see
    `_gitless_snapshot`). `config.tracker_dir(root)` resolves to that subdir, so
    `rebar.<read>(repo_root=root)` exercises the snapshot read path end to end."""
    repo = tmp_path / "snaproot"
    repo.mkdir()
    _gitless_snapshot(repo / ".tickets-tracker", with_event=with_event)
    return repo


@pytest.mark.parametrize(("name", "call"), _READ_ENTRY_POINTS)
def test_reads_raise_on_present_but_unusable_store_without_git(
    tmp_path: Path, name: str, call
) -> None:
    """A tracker dir that exists but holds no `.git` is a BROKEN store, read as an error."""
    repo = _tracker_present_without_git(tmp_path)

    with pytest.raises(RebarError) as excinfo:
        call(repo)

    assert rebar.error_code_for(excinfo.value) == "store_uninitialized", (
        f"{name} must report store_uninitialized for a `.git`-less tracker, got: {excinfo.value!r}"
    )


@pytest.mark.parametrize(("name", "call"), _READ_ENTRY_POINTS)
def test_reads_raise_on_midclone_store_unresolvable_head(tmp_path: Path, name: str, call) -> None:
    """A store mid-clone (`.git` present, HEAD unresolvable) reads as uninitialized, not `[]`."""
    repo = _tracker_midclone_unresolvable_head(tmp_path)

    with pytest.raises(RebarError) as excinfo:
        call(repo)

    assert rebar.error_code_for(excinfo.value) == "store_uninitialized", (
        f"{name} must report store_uninitialized for a mid-clone store, got: {excinfo.value!r}"
    )


def test_clone_without_env_id_still_reads(tmp_path: Path) -> None:
    """The read/write asymmetry the predicate must preserve: a HEAD-resolvable store missing
    its git-ignored `.env-id` still READS, while a WRITE is rejected by the `.env-id` gate."""
    repo = _clone_without_env_id(tmp_path)

    assert rebar.list_tickets(repo_root=repo) == []
    assert rebar.ready(repo_root=repo) == []

    with pytest.raises(RebarError):
        rebar.create_ticket("task", "probe", repo_root=repo)


def test_write_gate_rejects_midclone_store(tmp_path: Path) -> None:
    """The write gate now shares the predicate: `_ensure_initialized` must REJECT a mid-clone
    store (HEAD unresolvable) and still ACCEPT an initialized one. Under the old `.git`-only
    gate the mid-clone store passed, so read and write disagreed about it."""
    from rebar._store.event_prepare import StoreError, _ensure_initialized

    midclone = str(_tracker_midclone_unresolvable_head(tmp_path) / ".tickets-tracker")
    with pytest.raises(StoreError):
        _ensure_initialized(midclone)

    good = str(_initialized_empty_repo(tmp_path) / ".tickets-tracker")
    _ensure_initialized(good)  # must NOT raise


def test_store_is_usable_predicate(tmp_path: Path) -> None:
    """The shared predicate directly: False for absent / `.git`-less / mid-clone (incl. the
    walk-up shape), True for an initialized store and a HEAD-resolvable clone without `.env-id`."""
    from rebar._store.store_usability import store_is_usable

    bases = {name: tmp_path / name for name in ("a", "b", "c", "d", "e")}
    for base in bases.values():
        base.mkdir()

    absent = str(_bare_repo(bases["a"]) / ".tickets-tracker")
    no_git = str(_tracker_present_without_git(bases["b"]) / ".tickets-tracker")
    midclone = str(_tracker_midclone_unresolvable_head(bases["c"]) / ".tickets-tracker")
    initialized = str(_initialized_empty_repo(bases["d"]) / ".tickets-tracker")
    no_env_id = str(_clone_without_env_id(bases["e"]) / ".tickets-tracker")

    assert store_is_usable(absent) is False
    assert store_is_usable(no_git) is False
    assert store_is_usable(midclone) is False
    assert store_is_usable(initialized) is True
    assert store_is_usable(no_env_id) is True

    # Store-structure clause (the `.git`-less materialized-snapshot fix): a tracker with the
    # committed record OR a ticket event dir is usable even without `.git`; a bare directory
    # with neither is not.
    snap = str(_gitless_snapshot(bases["a"] / "snap"))
    events_only = str(_gitless_event_dir_only(bases["b"] / "evonly"))
    empty_snap = str(_gitless_snapshot(bases["c"] / "emptysnap", with_event=False))
    empty_dir = bases["d"] / "emptydir"
    empty_dir.mkdir()

    assert store_is_usable(snap) is True
    assert store_is_usable(events_only) is True
    assert store_is_usable(empty_snap) is True  # zero events, usable via the committed record
    assert store_is_usable(str(empty_dir)) is False


def test_store_is_usable_propagates_missing_git_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure taxonomy (advisory T5b): a CLEAN non-zero `rev-parse` is 'HEAD unresolvable'
    (-> not usable), but an OSError (git binary missing / environment fault) must PROPAGATE,
    not be silently collapsed into `store_uninitialized`. Otherwise a stripped deploy image
    with no `git` on PATH would report every store as uninitialized. The HEAD probe reuses the
    store's bounded runner (`gitutil.run_git_bounded`), which folds only TimeoutExpired, so an
    OSError from the underlying `run_git` propagates unchanged."""
    from rebar._store import gitutil
    from rebar._store.store_usability import store_is_usable

    tracker = str(_initialized_empty_repo(tmp_path) / ".tickets-tracker")

    def _fake_run_git(*_a, **_kw):
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    # `run_git_bounded` resolves `run_git` from the gitutil module global at call time.
    monkeypatch.setattr(gitutil, "run_git", _fake_run_git)

    with pytest.raises(OSError):
        store_is_usable(tracker)


def test_materialized_snapshot_reads_instead_of_raising(tmp_path: Path) -> None:
    """Regression fix: a `.git`-LESS materialized snapshot (a checked-out tickets tree with a
    committed `.store-compat.json` + a ticket event dir, exactly what
    `_snapshot.materialize_tickets` produces and the gate agents read) must READ, not raise
    `store_uninitialized`. A pure `.git`/HEAD predicate rejected it, breaking the gate-agent
    read surface."""
    snap = _gitless_snapshot_repo(tmp_path)

    tickets = rebar.list_tickets(repo_root=str(snap))
    assert [t["ticket_id"] for t in tickets] == ["abcd-1234-5678-9abc"]
    assert rebar.show_ticket("abcd-1234-5678-9abc", repo_root=str(snap))["title"] == "snap ticket"


def test_empty_materialized_snapshot_reads_empty(tmp_path: Path) -> None:
    """Operator edge ruling: a snapshot with ZERO events is still a VALID store via the
    committed `.store-compat.json` record (never keyed on emptiness), so it reads `[]`."""
    snap = _gitless_snapshot_repo(tmp_path, with_event=False)

    assert rebar.list_tickets(repo_root=str(snap)) == []


def test_store_structure_clause_is_load_bearing_for_snapshot_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seeded-mutation guard (Step 5): STRIP the store-structure clause and the `.git`-less
    snapshot read must return to RAISING `store_uninitialized`. This pins clause 2 as the thing
    that fixes the snapshot regression, so a future revert to a `.git`/HEAD-only predicate
    cannot silently reintroduce the break."""
    import rebar._store.store_usability as su

    snap = _gitless_snapshot_repo(tmp_path)

    # Mutation: neuter the store-structure clause (simulating a revert to `.git`/HEAD-only).
    monkeypatch.setattr(su, "_carries_store_structure", lambda _tracker: False)

    with pytest.raises(RebarError) as excinfo:
        rebar.list_tickets(repo_root=str(snap))
    assert rebar.error_code_for(excinfo.value) == "store_uninitialized"


def test_audit_index_renders_empty_and_logs_for_present_but_unusable_store(
    tmp_path: Path, caplog
) -> None:
    """The reconciled consumer (plan-review G6): widening `store_uninitialized` to cover a
    present-but-unusable store must not RE-mask it silently at the audit layer. The read-only
    web index still degrades to an empty page (it must not 500), but it now LOGS a warning so
    the broken store is not silently blanked."""
    import logging

    from rebar.audit.server import _audited_tickets

    repo = _tracker_present_without_git(tmp_path)

    with caplog.at_level(logging.WARNING):
        assert _audited_tickets(repo_root=str(repo)) == []

    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "a present-but-unusable store must be LOGGED, not silently blanked"
    )
