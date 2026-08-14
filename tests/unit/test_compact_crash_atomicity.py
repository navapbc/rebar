"""Crash-atomic compaction fold (bug compulsory-pernickety-mantis, e72e-259d-5ee7-4e73).

THE DEFECT. The fold mutated the SHARED tracker worktree in separate, unguarded steps —
SNAPSHOT write, source retirement (``*.retired`` renames), ``git add`` + commit — with no
whole-fold atomicity. A detached sweep worker that died, was SIGKILLed, or hit any exception
between the first mutation and the commit left the tree DIRTY (an untracked SNAPSHOT plus
tracked-file deletions), which then wedged every session sharing the store:
``sync._union_merge``'s ``git merge origin/tickets`` aborts on a dirty tree, stranding local
ticket commits off origin.

THE INVARIANT (asserted here): at every instant the tracker worktree is either in the
pre-fold state or the fully-committed post-fold state. A killed worker leaves nothing dirty
that the next sweep does not converge, and recovery is idempotent.

THE MECHANISM: an INTENT JOURNAL (commit-pending sentinel) written OUTSIDE the worktree
(under the tracker's git dir) before the first mutation, discarded on every completed
outcome. Recovery reverts ONLY a live journal whose recorded snapshot exists on disk and is
not in HEAD — so the intentionally-retained SNAPSHOT_INCONSISTENT state (which has no
journal by construction) is never touched, and ``fsck --repair-snapshots`` remains its sole
owner.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import compact as _compact
from rebar._commands import compact_recovery
from rebar._commands import compact_txn as _txn
from rebar.reducer import reduce_ticket

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SIGNING_KEY", "k")
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")
    rebar.init_repo(repo_root=str(repo))
    return repo


def _tracker(repo: Path) -> Path:
    return repo / ".tickets-tracker"


def _tdir(repo: Path, tid: str) -> Path:
    return _tracker(repo) / tid


def _status(repo: Path) -> str:
    """The tracker's ``git status --porcelain`` — the exact dirt that wedges reconverge."""
    return subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=_tracker(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _in_head(repo: Path, rel: str) -> bool:
    return (
        subprocess.run(
            ["git", "cat-file", "-e", f"HEAD:{rel}"],
            cwd=_tracker(repo),
            capture_output=True,
        ).returncode
        == 0
    )


def _seed(repo: Path, title: str, comments: int = 4) -> str:
    tid = rebar.create_ticket("task", title, description="x" * 60, repo_root=str(repo))
    for i in range(comments):
        rebar.comment(tid, f"c{i}", repo_root=str(repo))
    return tid


def _fold(repo: Path, tid: str, *extra: str) -> int:
    return _compact.compact_cli(
        [tid, "--threshold=0", "--horizon=0", "--skip-sync", *extra], repo_root=str(repo)
    )


def _snapshots(repo: Path, tid: str) -> list[Path]:
    return sorted(_tdir(repo, tid).glob("*-SNAPSHOT.json"))


def _actives(repo: Path, tid: str) -> list[Path]:
    return sorted(
        p
        for p in _tdir(repo, tid).glob("*.json")
        if not p.name.startswith(".")
        and not p.name.endswith("-SNAPSHOT.json")
        and not p.name.endswith("-SYNC.json")
    )


def _retired(repo: Path, tid: str) -> list[Path]:
    return sorted(_tdir(repo, tid).glob("*.retired"))


def _semantic(state: dict | None) -> dict:
    assert state is not None
    return {k: v for k, v in state.items() if k not in ("updated_at", "authorship_ledger")}


def _boom(*_a, **_k):
    raise RuntimeError("worker died mid-fold")


# ── an exception anywhere inside the fold rolls the WHOLE fold back ──────────────────────────
def test_exception_before_retirement_rolls_back_whole_fold(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Death after the SNAPSHOT write but before retirement: the tree must come back to the
    exact pre-fold state — no stray SNAPSHOT, no retired sources, porcelain-clean."""
    repo = store
    tid = _seed(repo, "dies before retiring")
    before = _semantic(reduce_ticket(str(_tdir(repo, tid))))
    n_active = len(_actives(repo, tid))

    monkeypatch.setattr(_txn, "_retire_folded_sources", _boom)
    with pytest.raises(RuntimeError):
        _fold(repo, tid)

    assert _status(repo) == "", "the crashed fold left the shared tracker tree dirty"
    assert not _snapshots(repo, tid), "the uncommitted SNAPSHOT must be rolled back"
    assert not _retired(repo, tid)
    assert len(_actives(repo, tid)) == n_active
    assert _semantic(reduce_ticket(str(_tdir(repo, tid)))) == before
    assert not compact_recovery.pending_intents(str(_tracker(repo))), (
        "a completed rollback must discard its intent journal"
    )


def test_commit_failure_rolls_back_whole_fold(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed ``git commit`` is the largest historical dirt window: SNAPSHOT written,
    sources retired, nothing committed. The fold must revert it all, not return 1 and walk
    away from a dirty tree."""
    repo = store
    tid = _seed(repo, "commit fails")
    before = _semantic(reduce_ticket(str(_tdir(repo, tid))))

    monkeypatch.setattr(_txn, "_commit_compaction", lambda _tracker, _tid: 1)
    assert _fold(repo, tid) == 1

    assert _status(repo) == "", "a failed commit left the fold's mutations dirty in the tree"
    assert not _snapshots(repo, tid)
    assert not _retired(repo, tid)
    assert _semantic(reduce_ticket(str(_tdir(repo, tid)))) == before
    assert not compact_recovery.pending_intents(str(_tracker(repo)))


# ── SIGKILL (no cleanup code runs): the journal converges the NEXT run ────────────────────────
def test_sigkill_leftover_converges_on_next_sweep(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Emulate SIGKILL by disabling the in-process abort path entirely — none of the
    rollback/journal-discard code runs, exactly as when the detached worker dies. The next
    sweep's recovery preamble must revert the partial fold and leave the tree clean, and a
    second recovery pass must be an idempotent no-op."""
    repo = store
    tid = _seed(repo, "sigkilled mid-fold")
    before = _semantic(reduce_ticket(str(_tdir(repo, tid))))

    with monkeypatch.context() as m:
        m.setattr(_txn, "_abort_fold", lambda *a, **k: None)  # SIGKILL: nothing runs
        m.setattr(_txn, "_commit_compaction", _boom)
        with pytest.raises(RuntimeError):
            _fold(repo, tid)

    tracker = str(_tracker(repo))
    assert _status(repo) != "", "precondition: the crashed fold left the tree dirty"
    assert compact_recovery.pending_intents(tracker), (
        "precondition: the crashed fold left its intent journal behind"
    )

    monkeypatch.setattr("rebar._store.push.push_after_commit", lambda *a, **k: None)
    assert _compact.compact_all_cli([], repo_root=str(repo)) == 0

    assert _status(repo) == "", "the sweep's recovery preamble did not converge the dirty tree"
    assert not compact_recovery.pending_intents(tracker)
    assert _semantic(reduce_ticket(str(_tdir(repo, tid)))) == before
    # Idempotent: a second recovery pass finds nothing to do and changes nothing.
    assert compact_recovery.recover_abandoned_folds_locked(tracker) == 0
    assert _status(repo) == ""


def test_recovery_discards_journal_when_snapshot_is_committed(store: Path) -> None:
    """A crash BETWEEN the commit and the journal discard: the fold actually landed, so
    recovery must NOT revert it — it just discards the stale journal."""
    repo = store
    tid = _seed(repo, "crashed after commit")
    assert _fold(repo, tid) == 0
    snap = _snapshots(repo, tid)[0]
    retired_before = _retired(repo, tid)
    assert retired_before, "precondition: the fold retired its sources"
    after = _semantic(reduce_ticket(str(_tdir(repo, tid))))

    tracker = str(_tracker(repo))
    fold_files = [str(p)[: -len(".retired")] for p in retired_before]
    compact_recovery.write_intent(tracker, tid, str(snap), fold_files)

    assert compact_recovery.recover_abandoned_folds_locked(tracker) == 0
    assert snap.exists(), "recovery reverted a fold that had already committed"
    assert _retired(repo, tid) == retired_before
    assert not compact_recovery.pending_intents(tracker)
    assert _status(repo) == ""
    assert _semantic(reduce_ticket(str(_tdir(repo, tid)))) == after


def test_recovery_discards_journal_when_snapshot_gone_from_disk(store: Path) -> None:
    """A journal whose snapshot is GONE from disk means another owner (e.g. ``fsck
    --repair-snapshots``) superseded the state: recovery must discard the journal and
    touch nothing — no revert, no resurrection of retired sources."""
    repo = store
    tid = _seed(repo, "superseded by fsck")
    assert _fold(repo, tid) == 0
    snap = _snapshots(repo, tid)[0]
    retired_before = _retired(repo, tid)
    assert retired_before, "precondition: the fold retired its sources"

    tracker = str(_tracker(repo))
    fold_files = [str(p)[: -len(".retired")] for p in retired_before]
    compact_recovery.write_intent(tracker, tid, str(snap), fold_files)
    snap.unlink()  # the superseding owner removed/replaced the journalled snapshot

    assert compact_recovery.recover_abandoned_folds_locked(tracker) == 0
    assert not compact_recovery.pending_intents(tracker)
    assert _retired(repo, tid) == retired_before, (
        "recovery reverted sources for a snapshot it no longer owns"
    )


def test_recovery_defers_when_head_probe_is_indeterminate(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient git fault (here: ``run_git_write``'s synthetic rc-124 timeout) must
    read as INDETERMINATE, not as "absent from HEAD" — misreading it would revert a fold
    that actually landed. Recovery keeps the journal and converges on the next pass."""
    repo = store
    tid = _seed(repo, "flaky git probe")
    before = _semantic(reduce_ticket(str(_tdir(repo, tid))))
    tracker = str(_tracker(repo))
    with monkeypatch.context() as m:
        m.setattr(_txn, "_abort_fold", lambda *a, **k: None)  # SIGKILL: nothing runs
        m.setattr(_txn, "_commit_compaction", _boom)
        with pytest.raises(RuntimeError):
            _fold(repo, tid)
    assert compact_recovery.pending_intents(tracker), "precondition: journal left behind"

    class _Timeout:
        returncode = 124

    real_git = compact_recovery._git

    def flaky_git(trk: str, *args: str):
        if args[0] == "cat-file":
            return _Timeout()
        return real_git(trk, *args)

    with monkeypatch.context() as m:
        m.setattr(compact_recovery, "_git", flaky_git)
        assert compact_recovery.recover_abandoned_folds(tracker) == 0
    assert compact_recovery.pending_intents(tracker), (
        "an indeterminate HEAD probe must KEEP the journal for the next preamble"
    )
    # The next (healthy) pass converges the abandoned fold.
    assert compact_recovery.recover_abandoned_folds(tracker) == 1
    assert not compact_recovery.pending_intents(tracker)
    assert _status(repo) == ""
    assert _semantic(reduce_ticket(str(_tdir(repo, tid)))) == before


def test_apply_fold_refuses_to_mutate_when_journal_unwritable(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the intent journal cannot be written the fold must REFUSE before its first
    worktree mutation: an unjournaled crash is exactly the wedge this bug is about."""
    repo = store
    tid = _seed(repo, "unjournalable fold")
    before = _semantic(reduce_ticket(str(_tdir(repo, tid))))
    actives_before = _actives(repo, tid)

    monkeypatch.setattr(
        compact_recovery, "write_intent", lambda *a, **k: (_ for _ in ()).throw(OSError("ro"))
    )
    assert _fold(repo, tid) == 1

    assert _status(repo) == ""
    assert not _snapshots(repo, tid), "the refused fold must not have written its SNAPSHOT"
    assert not _retired(repo, tid)
    assert _actives(repo, tid) == actives_before
    assert _semantic(reduce_ticket(str(_tdir(repo, tid)))) == before


def test_rollback_retains_snapshot_when_reverse_rename_fails(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The data-loss guard: when a reverse-rename fails mid-rollback, a source is stuck as
    ``*.retired`` and its folded effect lives ONLY in the SNAPSHOT — deleting the SNAPSHOT
    then would silently drop events. ``_rollback_fold`` must RETAIN it (the documented
    SNAPSHOT_INCONSISTENT state owned by ``fsck --repair-snapshots``)."""
    repo = store
    tid = _seed(repo, "stuck reverse rename")
    tracker = str(_tracker(repo))
    # Reconstruct the mid-fold state _rollback_fold sees: snapshot written, sources retired.
    fold_files = [str(p) for p in _actives(repo, tid)]
    snap = _tdir(repo, tid) / "x-SNAPSHOT.json"
    snap.write_text("{}", encoding="utf-8")
    for fp in fold_files:
        Path(fp).rename(fp + ".retired")

    real_rename = _txn.os.rename

    def stuck_rename(src, dst, *a, **k):
        if str(src).endswith(".retired"):
            raise OSError("stuck")
        return real_rename(src, dst, *a, **k)

    monkeypatch.setattr(_txn.os, "rename", stuck_rename)
    _txn._rollback_fold(tracker, tid, str(snap), fold_files)

    assert snap.exists(), (
        "rollback deleted the SNAPSHOT while sources were stuck retired — data loss"
    )
    assert _retired(repo, tid), "precondition: the reverse-renames were blocked"
    # And the clean path removes it: with renames working, the same rollback converges.
    monkeypatch.setattr(_txn.os, "rename", real_rename)
    _txn._rollback_fold(tracker, tid, str(snap), fold_files)
    assert not snap.exists()
    assert not _retired(repo, tid)
    assert _status(repo) == ""


def test_recovery_defers_when_head_is_unresolvable(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other indeterminate branch: when HEAD itself cannot be resolved, the probe must
    answer None (defer) rather than misread the repo fault as absent-from-HEAD."""
    repo = store
    tid = _seed(repo, "unresolvable head")
    tracker = str(_tracker(repo))
    with monkeypatch.context() as m:
        m.setattr(_txn, "_abort_fold", lambda *a, **k: None)  # SIGKILL: nothing runs
        m.setattr(_txn, "_commit_compaction", _boom)
        with pytest.raises(RuntimeError):
            _fold(repo, tid)
    assert compact_recovery.pending_intents(tracker), "precondition: journal left behind"

    class _Fatal:
        returncode = 128

    real_git = compact_recovery._git

    def broken_head(trk: str, *args: str):
        if args[0] == "rev-parse":
            return _Fatal()
        return real_git(trk, *args)

    with monkeypatch.context() as m:
        m.setattr(compact_recovery, "_git", broken_head)
        assert compact_recovery.recover_abandoned_folds(tracker) == 0
    assert compact_recovery.pending_intents(tracker), (
        "an unresolvable HEAD must KEEP the journal for the next preamble"
    )
    assert compact_recovery.recover_abandoned_folds(tracker) == 1
    assert _status(repo) == ""


def test_recovery_retains_snapshot_when_reverse_rename_fails(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cross-process twin of the ``_rollback_fold`` data-loss guard: when recovery
    cannot restore a ``*.retired`` source, its folded effect lives ONLY in the snapshot —
    ``_revert_partial_fold`` must retain the snapshot for ``fsck --repair-snapshots``."""
    repo = store
    tid = _seed(repo, "recovery stuck rename")
    tracker = str(_tracker(repo))
    with monkeypatch.context() as m:
        m.setattr(_txn, "_abort_fold", lambda *a, **k: None)  # SIGKILL: nothing runs
        m.setattr(_txn, "_commit_compaction", _boom)
        with pytest.raises(RuntimeError):
            _fold(repo, tid)
    assert compact_recovery.pending_intents(tracker), "precondition: journal left behind"
    snap = _snapshots(repo, tid)[0]

    real_rename = compact_recovery.os.rename

    def stuck_rename(src, dst, *a, **k):
        if str(src).endswith(".retired"):
            raise OSError("stuck")
        return real_rename(src, dst, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(compact_recovery.os, "rename", stuck_rename)
        assert compact_recovery.recover_abandoned_folds(tracker) == 1
    assert snap.exists(), (
        "recovery deleted the snapshot while sources were stuck retired — data loss"
    )
    assert _retired(repo, tid), "precondition: the reverse-renames were blocked"
    assert not compact_recovery.pending_intents(tracker), (
        "the retained SNAPSHOT_INCONSISTENT state belongs to fsck; the journal must not survive"
    )


def test_lock_unavailable_defers_recovery_without_failing(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A busy/incompatible store must DEFER recovery — return 0, keep the journal, never
    raise into the caller that ran the preamble."""
    repo = store
    tid = _seed(repo, "busy store")
    tracker = str(_tracker(repo))
    compact_recovery.write_intent(tracker, tid, str(_tdir(repo, tid) / "x-SNAPSHOT.json"), [])

    monkeypatch.setattr(
        "rebar._store.lock.acquire",
        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("store busy")),
    )
    assert compact_recovery.recover_abandoned_folds_locked(tracker) == 0
    assert compact_recovery.pending_intents(tracker), (
        "deferred recovery must leave the journal intact for the next caller"
    )


def test_recovery_never_touches_snapshot_inconsistent_without_journal(store: Path) -> None:
    """The intentionally-retained SNAPSHOT_INCONSISTENT state (incomplete rollback in
    ``_retire_folded_sources``) carries NO journal by construction — recovery must leave it
    for ``fsck --repair-snapshots``, never "helpfully" delete the retained SNAPSHOT."""
    repo = store
    tid = _seed(repo, "fsck territory")
    assert _fold(repo, tid) == 0
    # Reconstruct the mixed state: SNAPSHOT present, folded sources back to active.
    for p in _retired(repo, tid):
        p.rename(p.with_name(p.name[: -len(".retired")]))
    snap = _snapshots(repo, tid)[0]
    actives = _actives(repo, tid)

    tracker = str(_tracker(repo))
    assert compact_recovery.recover_abandoned_folds_locked(tracker) == 0
    assert snap.exists(), "recovery deleted a retained SNAPSHOT it does not own"
    assert _actives(repo, tid) == actives


# ── a successful fold commits as ONE unit and leaves no sentinel ──────────────────────────────
def test_successful_fold_commits_and_leaves_no_journal(store: Path) -> None:
    repo = store
    tid = _seed(repo, "healthy fold")
    before = _semantic(reduce_ticket(str(_tdir(repo, tid))))

    assert _fold(repo, tid) == 0

    snaps = _snapshots(repo, tid)
    assert len(snaps) == 1
    assert _in_head(repo, f"{tid}/{snaps[0].name}"), "the fold's SNAPSHOT must be committed"
    assert _status(repo) == ""
    assert not compact_recovery.pending_intents(str(_tracker(repo)))
    assert _semantic(reduce_ticket(str(_tdir(repo, tid)))) == before


# ── the sweep commits each fold as its own unit (no batch-wide dirt window) ──────────────────
def test_sweep_tree_is_committed_between_folds(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep used to fold every ticket with ``--no-commit`` and batch ONE commit at the
    end, so a worker killed mid-sweep stranded the entire batch as dirt. Each fold must now
    commit its own unit: observed at the yield point BETWEEN folds, the tree is clean."""
    repo = store
    for n in range(2):
        _seed(repo, f"sweep {n}")

    seen: dict[str, str] = {}

    def yield_after_first(_tracker: str, done: int) -> bool:
        if done >= 1:
            seen.setdefault("status", _status(repo))
            return True
        return False

    monkeypatch.setattr(_compact, "_sweep_should_yield", yield_after_first)
    monkeypatch.setattr("rebar._store.push.push_after_commit", lambda *a, **k: None)
    assert _compact.compact_all_cli([], repo_root=str(repo)) == 0

    assert seen.get("status") == "", (
        "mid-sweep the tracker tree was dirty — a killed worker would strand the batch:\n"
        + seen.get("status", "<yield point never reached>")
    )
    assert _status(repo) == ""


def test_operator_no_commit_is_preserved_and_writes_no_journal(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``compact-all --no-commit`` is an explicit request for an uncommitted fold: it must
    keep working, and it must NOT write a journal — otherwise the next recovery preamble
    would revert the operator's intentional state."""
    repo = store
    tid = _seed(repo, "no-commit fold")

    monkeypatch.setattr("rebar._store.push.push_after_commit", lambda *a, **k: None)
    assert _compact.compact_all_cli(["--no-commit"], repo_root=str(repo)) == 0

    assert _snapshots(repo, tid), "--no-commit must still fold"
    assert _status(repo) != "", "--no-commit means the fold stays uncommitted by request"
    assert not compact_recovery.pending_intents(str(_tracker(repo))), (
        "a --no-commit fold journaled an intent; recovery would revert the operator's state"
    )


def test_crash_state_is_json_round_trippable(store: Path) -> None:
    """The journal is read back by a DIFFERENT process after a crash — pin its shape."""
    repo = store
    tid = _seed(repo, "journal shape")
    tracker = str(_tracker(repo))
    files = [str(p) for p in _actives(repo, tid)]
    journal = compact_recovery.write_intent(
        tracker, tid, str(_tdir(repo, tid) / "x-SNAPSHOT.json"), files
    )
    record = json.loads(Path(journal).read_text(encoding="utf-8"))
    assert record["ticket_id"] == tid
    assert record["snapshot"] == f"{tid}/x-SNAPSHOT.json"
    assert record["sources"] == [f"{tid}/{Path(f).name}" for f in files]
    compact_recovery.discard(journal)
    assert not compact_recovery.pending_intents(tracker)
