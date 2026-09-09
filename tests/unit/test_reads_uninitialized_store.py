"""Library reads distinguish an unusable ticket store from a valid empty store.

All public read entry points share the usability guard, including MCP delegates. Absent,
broken, and mid-clone stores raise ``store_uninitialized``; initialized empty stores return
``[]``. Structured read-only snapshots remain readable, while writes retain stricter git and
provenance requirements.
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
    """The best-effort audit index renders an absent store as an empty page, not a 500."""
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


# Public reads accept live git stores and structured snapshots, but reject absent, broken, or
# mid-clone stores. Writes and the audit index apply their stricter contracts separately.

_READ_ENTRY_POINTS = [
    ("list_tickets", lambda repo: rebar.list_tickets(repo_root=repo)),
    ("search", lambda repo: rebar.search("anything", repo_root=repo)),
    ("ready", lambda repo: rebar.ready(repo_root=repo)),
    ("recent_session_logs", lambda repo: rebar.recent_session_logs(repo_root=repo)),
    ("deps", lambda repo: rebar.deps("abcd-1234-5678-9abc", repo_root=repo)),
    ("next_batch", lambda repo: rebar.next_batch("abcd-1234-5678-9abc", repo_root=repo)),
]


def _tracker_present_without_git(tmp_path: Path) -> Path:
    """Return a repository containing a nested tracker whose local ``.git`` is absent.

    The enclosing git repository ensures the predicate rejects locally before a command can
    walk up and resolve the wrong HEAD.
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
    """Return a repository containing a tracker with ``.git`` but an unresolvable HEAD."""
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
    """Return a readable clone without its local ``.env-id`` provenance stamp.

    Reads require a resolvable git store; writes additionally require the environment identity.
    """
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
    """Build a ``.git``-less snapshot with a compatibility record and optional event data."""
    tracker.mkdir(parents=True, exist_ok=True)
    (tracker / ".store-compat.json").write_text(_STORE_COMPAT_RECORD)
    if with_event:
        _write_create_event(tracker / "abcd-1234-5678-9abc", "abcd-1234-5678-9abc", "snap ticket")
    assert not (tracker / ".git").exists()
    return tracker


def _gitless_event_dir_only(tracker: Path) -> Path:
    """Build a legacy snapshot recognized solely by its ticket event directory."""
    tracker.mkdir(parents=True, exist_ok=True)
    _write_create_event(tracker / "abcd-1234-5678-9abc", "abcd-1234-5678-9abc", "legacy ticket")
    assert not (tracker / ".git").exists() and not (tracker / ".store-compat.json").exists()
    return tracker


def _gitless_snapshot_repo(tmp_path: Path, *, with_event: bool = True) -> Path:
    """Wrap a materialized tracker snapshot in a repository root for public read calls."""
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
    """A resolvable clone without ``.env-id`` stays readable but rejects writes."""
    repo = _clone_without_env_id(tmp_path)

    assert rebar.list_tickets(repo_root=repo) == []
    assert rebar.ready(repo_root=repo) == []

    with pytest.raises(RebarError):
        rebar.create_ticket("task", "probe", repo_root=repo)


def test_write_gate_rejects_midclone_store(tmp_path: Path) -> None:
    """Writes reject an unresolvable mid-clone while accepting an initialized store."""
    from rebar._store.event_prepare import StoreError, _ensure_initialized

    midclone = str(_tracker_midclone_unresolvable_head(tmp_path) / ".tickets-tracker")
    with pytest.raises(StoreError):
        _ensure_initialized(midclone)

    good = str(_initialized_empty_repo(tmp_path) / ".tickets-tracker")
    _ensure_initialized(good)  # must NOT raise


def test_write_gate_rejects_gitless_snapshot_that_reads_fine(tmp_path: Path) -> None:
    """Materialized snapshots are readable but remain unwritable without a git repository."""
    from rebar._store.event_prepare import StoreError, _ensure_initialized
    from rebar._store.store_usability import store_is_usable, store_is_writable

    snap = str(_gitless_snapshot_repo(tmp_path) / ".tickets-tracker")

    assert store_is_usable(snap) is True  # readable
    assert store_is_writable(snap) is False  # but NOT writable (no .git)
    with pytest.raises(StoreError):
        _ensure_initialized(snap)


def test_store_is_usable_predicate(tmp_path: Path) -> None:
    """Usability accepts initialized clones and snapshots, but rejects absent or broken stores."""
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

    # Error-handling hardening: a `.git`-less directory whose only non-dot subdirectory holds NO
    # event `.json` is NOT store structure — a stray/unrelated dir must not read as a store, and a
    # mid-clone store whose empty ticket dirs exist before their events are checked out still fails.
    stray = bases["e"] / "stray"
    (stray / "not-a-ticket-dir").mkdir(parents=True)
    assert store_is_usable(str(stray)) is False


def test_structured_store_usability_does_not_spawn_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An initialized store carrying committed structure is usable without probing HEAD.

    The HEAD probe is only needed for live git stores whose structure has not landed yet
    (for example a mid-clone store). A normal initialized store already carries the durable
    compatibility record, so every read must not pay for an extra git subprocess.
    """
    from rebar._store import store_usability

    tracker = str(_initialized_empty_repo(tmp_path) / ".tickets-tracker")

    def _forbidden_head_probe(*_args, **_kwargs):
        raise AssertionError("structured stores must not spawn git to prove usability")

    monkeypatch.setattr(store_usability, "run_git_bounded", _forbidden_head_probe)

    assert store_usability.store_is_usable(tracker) is True


def test_store_is_usable_propagates_missing_git_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing git executable propagates instead of masquerading as an unusable store."""
    from rebar._store import gitutil
    from rebar._store.store_usability import store_is_usable

    tracker = str(_tracker_midclone_unresolvable_head(tmp_path) / ".tickets-tracker")

    def _fake_run_git(*_a, **_kw):
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    # `run_git_bounded` resolves `run_git` from the gitutil module global at call time.
    monkeypatch.setattr(gitutil, "run_git", _fake_run_git)

    with pytest.raises(OSError):
        store_is_usable(tracker)


def test_materialized_snapshot_reads_instead_of_raising(tmp_path: Path) -> None:
    """A structured ``.git``-less snapshot serves public reads used by gate agents."""
    snap = _gitless_snapshot_repo(tmp_path)

    tickets = rebar.list_tickets(repo_root=str(snap))
    assert [t["ticket_id"] for t in tickets] == ["abcd-1234-5678-9abc"]
    assert rebar.show_ticket("abcd-1234-5678-9abc", repo_root=str(snap))["title"] == "snap ticket"


def test_empty_materialized_snapshot_reads_empty(tmp_path: Path) -> None:
    """A compatibility record makes an eventless snapshot a valid empty store."""
    snap = _gitless_snapshot_repo(tmp_path, with_event=False)

    assert rebar.list_tickets(repo_root=str(snap)) == []


def test_store_structure_clause_is_load_bearing_for_snapshot_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabling store-structure detection makes ``.git``-less snapshot reads fail."""
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
    """The audit index renders an unusable store empty and logs the condition."""
    import logging

    from rebar.audit.server import _audited_tickets

    repo = _tracker_present_without_git(tmp_path)

    with caplog.at_level(logging.WARNING):
        assert _audited_tickets(repo_root=str(repo)) == []

    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "a present-but-unusable store must be LOGGED, not silently blanked"
    )
