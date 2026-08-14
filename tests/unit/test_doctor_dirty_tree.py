"""The dirty-tracker wedge class: ``fsck`` DETECTS it, ``doctor --repair`` HEALS it
(ticket c925-7669-ded8-43a3).

The wedge shape (live P0s 6ccc-0577-198c-44fa / e72e-259d-5ee7-4e73): the tickets
tracker working tree carries (1) tracked deletions of store artifacts whose bytes are
intact at HEAD, (2) untracked regenerable compaction leftovers (``*-SNAPSHOT.json``,
``*.retired`` whose retired-source is already folded), and (3) orphaned ``.tmp-event-*``
staging files — while origin has diverged, so every union merge aborts and local ticket
commits strand off origin.

Pinned here:

* classification fences — a plain untracked ``.json`` (a live event pending append) and
  a ``.retired`` whose source is preserved NOWHERE committed are never classified;
* the fsck text/JSON contract — each class gets a distinct finding kind carrying count
  and paths;
* the doctor repair protocol — backup ref before the first mutation, ONE short write-lock
  window for the file mutations (restore / quarantine-move, never delete), lock RELEASED
  before ``sync.reconverge`` (which self-locks; the store write lock is non-reentrant),
  class 3 untouched;
* idempotence — a clean store makes zero changes, zero commits, and no backup ref.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

_TMP_EVENT_NAME = ".tmp-event-orphan1234"
_TMP_EVENT_BYTES = b'{"half": "written event bytes'


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )


def _new_tickets_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "tickets", str(path)], check=True)
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _write(repo: Path, rel: str, body: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# Store-artifact paths, one per seeded state.
_DEL_WORKTREE = "aaaa-tick-1111-1111/1700000000000000001-aaaa-tick-1111-1111-CREATE.json"
_DEL_STAGED = "aaaa-tick-1111-1111/1700000000000000002-aaaa-tick-1111-1111-COMMENT.json"
_FOLD_SRC = "cccc-fold-3333-3333/1700000000000000003-cccc-fold-3333-3333-CREATE.json"
_LEFTOVER_SNAPSHOT = "cccc-fold-3333-3333/1700000000000000009-cccc-fold-3333-3333-SNAPSHOT.json"
_ORIGIN_RETIRED = "bbbb-peer-2222-2222/1700000000000000004-bbbb-peer-2222-2222-CREATE.json.retired"


@pytest.fixture
def wedged_store(tmp_path: Path) -> dict:
    """A diverged tracker seeded with all three dirty classes.

    origin: base commit, then a peer's committed compaction fold (a ``.retired`` file).
    tracker: cloned at base, then a local-only commit (diverged), then the dirt:

    * class 1 — a worktree deletion (`` D``) and a staged deletion (``D ``) of tracked
      event files (the P0 shape: bytes intact at HEAD);
    * class 2 — a crashed local fold (source renamed to ``.retired``: the ``.retired``
      is untracked and its source is preserved at HEAD), an untracked ``*-SNAPSHOT.json``,
      and an untracked ``.retired`` whose byte-source origin has already committed;
    * class 3 — an orphaned ``.tmp-event-*`` staging file at the tracker root.
    """
    origin = tmp_path / "origin"
    tracker = tmp_path / "tracker"
    _new_tickets_repo(origin)
    # The store's own .gitignore (as `rebar init` seeds) so lock/reducer artifacts our
    # own machinery creates during the test never show up as tracker dirt.
    _write(origin, ".gitignore", ".ticket-write.lock\n.ticket-write.lock.d/\n.cache.json\n")
    _write(origin, _DEL_WORKTREE, '{"e": "create-a"}')
    _write(origin, _DEL_STAGED, '{"e": "comment-a"}')
    _write(origin, _FOLD_SRC, '{"e": "create-c"}')
    _commit_all(origin, "base")

    subprocess.run(["git", "clone", "-q", "-b", "tickets", str(origin), str(tracker)], check=True)
    _git(tracker, "config", "user.email", "t@t")
    _git(tracker, "config", "user.name", "t")

    # Origin advances: a peer's compaction fold commits a retired event file. The
    # tracker has FETCHED it (a real wedge has: sync fetches before every merge attempt).
    _write(origin, _ORIGIN_RETIRED, '{"e": "peer-folded"}')
    origin_sha = _commit_all(origin, "peer fold")
    _git(tracker, "fetch", "-q", "origin")

    # Tracker diverges with a local-only commit.
    _write(tracker, "dddd-locl-4444-4444/1700000000000000005-dddd-locl-4444-4444-CREATE.json", "{}")
    local_sha = _commit_all(tracker, "local event")

    # class 1: tracked deletions (worktree + staged).
    (tracker / _DEL_WORKTREE).unlink()
    _git(tracker, "rm", "-q", "--", _DEL_STAGED)

    # class 2: crashed fold (rename source -> .retired: ` D` source + `??` retired
    # whose source is preserved at HEAD) …
    (tracker / _FOLD_SRC).rename(tracker / (_FOLD_SRC + ".retired"))
    # … an untracked snapshot leftover …
    _write(tracker, _LEFTOVER_SNAPSHOT, '{"snapshot": "regenerable"}')
    # … and an untracked .retired whose bytes origin has already committed.
    _write(tracker, _ORIGIN_RETIRED, '{"e": "peer-folded"}')

    # class 3: an orphaned staging file (report-only; never auto-touched).
    (tracker / _TMP_EVENT_NAME).write_bytes(_TMP_EVENT_BYTES)

    return {"origin": origin, "tracker": tracker, "origin_sha": origin_sha, "local_sha": local_sha}


def _clean_clone(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    tracker = tmp_path / "tracker"
    _new_tickets_repo(origin)
    _write(origin, ".gitignore", ".ticket-write.lock\n.ticket-write.lock.d/\n.cache.json\n")
    _write(origin, _DEL_WORKTREE, '{"e": "create-a"}')
    _commit_all(origin, "base")
    subprocess.run(["git", "clone", "-q", "-b", "tickets", str(origin), str(tracker)], check=True)
    _git(tracker, "config", "user.email", "t@t")
    _git(tracker, "config", "user.name", "t")
    return tracker


# ---------------------------------------------------------------------------
# detection — the classifier and its fences
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_classifier_names_each_seeded_class(wedged_store: dict) -> None:
    from rebar._commands.fsck_tracker_health import dirty_tracker_classes

    classes = dirty_tracker_classes(str(wedged_store["tracker"]))

    assert sorted(classes["deletions"]) == sorted([_DEL_WORKTREE, _DEL_STAGED, _FOLD_SRC])
    assert sorted(classes["leftovers"]) == sorted(
        [_FOLD_SRC + ".retired", _LEFTOVER_SNAPSHOT, _ORIGIN_RETIRED]
    )
    assert classes["tmp_events"] == [_TMP_EVENT_NAME]


@pytest.mark.unit
def test_classifier_fences(tmp_path: Path) -> None:
    """Never classified: a plain untracked .json (a live event pending append), and a
    .retired whose source bytes are preserved NOWHERE committed (quarantining it could
    orphan the only copy of an event)."""
    from rebar._commands.fsck_tracker_health import dirty_tracker_classes

    tracker = _clean_clone(tmp_path)
    _write(tracker, "eeee-live-5555-5555/1700000000000000006-eeee-live-5555-5555-CREATE.json", "{}")
    _write(tracker, "ffff-nowh-6666-6666/1700000000000000007-nowhere.json.retired", "{}")

    classes = dirty_tracker_classes(str(tracker))
    assert classes == {"deletions": [], "leftovers": [], "tmp_events": []}


@pytest.mark.unit
def test_classifier_is_empty_on_a_clean_tracker(tmp_path: Path) -> None:
    from rebar._commands.fsck_tracker_health import dirty_tracker_classes

    assert dirty_tracker_classes(str(_clean_clone(tmp_path))) == {
        "deletions": [],
        "leftovers": [],
        "tmp_events": [],
    }


@pytest.mark.unit
def test_fsck_reports_each_class_in_text_and_json(
    wedged_store: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The full fsck command names each class with a distinct kind, in text AND in
    --output json where each finding carries the per-class count and paths."""
    from rebar._commands import fsck

    tracker = wedged_store["tracker"]
    monkeypatch.setattr("rebar.config.tracker_dir", lambda _repo_root=None: tracker)

    rc = fsck.fsck_cli([], no_mutate=True)
    text = capsys.readouterr().out
    assert rc == 1
    assert "TRACKER_DIRTY_DELETION:" in text
    assert "TRACKER_DIRTY_LEFTOVER:" in text
    assert "TRACKER_DIRTY_TMP_EVENT:" in text

    rc = fsck.fsck_cli(["--output", "json"], no_mutate=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    by_kind = {}
    for item in payload["issues"]:
        by_kind.setdefault(item["kind"], item)
    deletion = by_kind["tracker_dirty_deletion"]
    assert deletion["count"] == 3
    assert sorted(deletion["paths"]) == sorted([_DEL_WORKTREE, _DEL_STAGED, _FOLD_SRC])
    leftover = by_kind["tracker_dirty_leftover"]
    assert leftover["count"] == 3
    assert sorted(leftover["paths"]) == sorted(
        [_FOLD_SRC + ".retired", _LEFTOVER_SNAPSHOT, _ORIGIN_RETIRED]
    )
    tmp = by_kind["tracker_dirty_tmp_event"]
    assert tmp["count"] == 1
    assert tmp["paths"] == [_TMP_EVENT_NAME]


@pytest.mark.unit
def test_fsck_reports_no_dirty_findings_on_a_clean_tracker(tmp_path: Path) -> None:
    from rebar._commands.fsck_tracker_health import _dirty_tracker_lines

    assert _dirty_tracker_lines(str(_clean_clone(tmp_path))) == []


@pytest.mark.unit
def test_dirty_deletion_and_leftover_are_counted_issues_tmp_event_is_not(
    wedged_store: dict,
) -> None:
    """Classes 1 and 2 are counted integrity issues. Class 3 is informational: an
    in-flight append legitimately holds a live ``.tmp-event-*`` for a moment, so
    counting it would make fsck flake against concurrent writers."""
    from rebar._commands.fsck_tracker_health import _dirty_tracker_lines

    lines = dict(_dirty_tracker_lines(str(wedged_store["tracker"])))
    by_kind = {line.split(":", 1)[0]: is_issue for line, is_issue in lines.items()}
    assert by_kind["TRACKER_DIRTY_DELETION"] is True
    assert by_kind["TRACKER_DIRTY_LEFTOVER"] is True
    assert by_kind["TRACKER_DIRTY_TMP_EVENT"] is False


# ---------------------------------------------------------------------------
# repair — doctor --repair heals the wedge end-to-end
# ---------------------------------------------------------------------------


def _patch_doctor(monkeypatch: pytest.MonkeyPatch, tracker: Path):
    from rebar._commands import doctor

    monkeypatch.setattr(doctor, "tracker_dir", lambda _repo_root=None: tracker)
    monkeypatch.setattr(doctor, "_reconciler_in_flight", lambda *_a, **_k: False)
    return doctor


@pytest.mark.unit
def test_doctor_repair_heals_the_seeded_wedge_end_to_end(
    wedged_store: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tracker = wedged_store["tracker"]
    doctor = _patch_doctor(monkeypatch, tracker)
    pre_head = _git(tracker, "rev-parse", "HEAD").stdout.strip()

    rc = doctor.doctor_cli(["--repair", "--output", "json"])
    payload = json.loads(capsys.readouterr().out)

    # class 1 restored from HEAD — bytes back, byte-identical to the committed copy.
    for rel in (_DEL_WORKTREE, _DEL_STAGED, _FOLD_SRC):
        assert (tracker / rel).exists(), f"class-1 path not restored: {rel}"
        head_bytes = _git(tracker, "show", f"{pre_head}:{rel}").stdout
        assert (tracker / rel).read_text(encoding="utf-8") == head_bytes

    # class 2 quarantined — moved, never deleted: bytes preserved under
    # <git-common-dir>/reconverge-quarantine/<utc-ts>/. The origin-committed .retired
    # comes BACK as a tracked file via the union merge (origin's committed copy); the
    # purely-local leftovers stay gone from the tree.
    quarantine_root = tracker / ".git" / "reconverge-quarantine"
    for rel in (_FOLD_SRC + ".retired", _LEFTOVER_SNAPSHOT, _ORIGIN_RETIRED):
        matches = list(quarantine_root.glob(f"*/{rel}"))
        assert matches, f"class-2 path missing from quarantine: {rel}"
    for rel in (_FOLD_SRC + ".retired", _LEFTOVER_SNAPSHOT):
        assert not (tracker / rel).exists(), f"class-2 path not quarantined: {rel}"
    assert _git(tracker, "ls-files", "--", _ORIGIN_RETIRED).stdout.strip() == _ORIGIN_RETIRED, (
        "origin's committed .retired did not land as a tracked file after the merge"
    )

    # class 3 untouched — byte-identical.
    assert (tracker / _TMP_EVENT_NAME).read_bytes() == _TMP_EVENT_BYTES

    # The store converged: the union merge kept BOTH sides.
    origin_sha, local_sha = wedged_store["origin_sha"], wedged_store["local_sha"]
    assert _git(tracker, "merge-base", "--is-ancestor", origin_sha, "HEAD").returncode == 0
    assert _git(tracker, "merge-base", "--is-ancestor", local_sha, "HEAD").returncode == 0
    # The tree is clean apart from class 3.
    status = _git(tracker, "status", "--porcelain", "-uall").stdout.splitlines()
    assert status == [f"?? {_TMP_EVENT_NAME}"], status

    # Backup ref recorded at the pre-repair tickets HEAD, before the first mutation.
    ref_probe = _git(tracker, "for-each-ref", "--format=%(objectname)", "refs/rebar-doctor/")
    refs = ref_probe.stdout.split()
    assert refs == [pre_head], refs

    # JSON findings carry the classes, their repair statuses, and the backup ref.
    by_kind = {f["kind"]: f for f in payload["findings"]}
    assert by_kind["tracker-dirty-deletion"]["repair_status"] == "repaired"
    assert by_kind["tracker-dirty-leftover"]["repair_status"] == "repaired"
    assert by_kind["tracker-dirty-tmp-event"]["repair_status"] == "manual"
    assert by_kind["tracker-dirty-deletion"]["backup_ref"].startswith("refs/rebar-doctor/")
    assert sorted(by_kind["tracker-dirty-tmp-event"]["paths"]) == [_TMP_EVENT_NAME]
    # Everything actionable was repaired; the class-3 triage notice does not fail the run.
    assert rc == 0


@pytest.mark.unit
def test_doctor_repair_is_idempotent_on_a_clean_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A clean store: zero changes, zero commits, no backup ref, no pre-repair tag."""
    tracker = _clean_clone(tmp_path)
    doctor = _patch_doctor(monkeypatch, tracker)
    pre_head = _git(tracker, "rev-parse", "HEAD").stdout.strip()

    rc = doctor.doctor_cli(["--repair"])
    capsys.readouterr()

    assert rc == 0
    assert _git(tracker, "rev-parse", "HEAD").stdout.strip() == pre_head
    assert _git(tracker, "for-each-ref", "refs/rebar-doctor/").stdout == ""
    assert _git(tracker, "tag", "-l", doctor.PRE_REPAIR_TAG).stdout.strip() == ""
    assert _git(tracker, "status", "--porcelain", "-uall").stdout == ""


@pytest.mark.unit
def test_class3_only_store_gets_no_mutation_and_no_backup_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Orphaned .tmp-event-* files are named for manual triage, never auto-touched:
    no backup ref, no lock window, no reconverge, bytes identical after the run."""
    tracker = _clean_clone(tmp_path)
    (tracker / _TMP_EVENT_NAME).write_bytes(_TMP_EVENT_BYTES)
    doctor = _patch_doctor(monkeypatch, tracker)
    reconverged = []
    monkeypatch.setattr(doctor, "_reconverge", lambda *_a: reconverged.append(True))

    doctor.doctor_cli(["--repair"])
    out = capsys.readouterr().out

    assert "tracker-dirty-tmp-event" in out
    assert _TMP_EVENT_NAME in out
    assert (tracker / _TMP_EVENT_NAME).read_bytes() == _TMP_EVENT_BYTES
    assert _git(tracker, "for-each-ref", "refs/rebar-doctor/").stdout == ""
    assert reconverged == []


@pytest.mark.unit
def test_repair_lock_discipline_two_windows(
    wedged_store: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write lock IS held for the file mutations (restore + quarantine move) and is
    RELEASED before reconverge is entered — reconverge takes the non-reentrant write
    lock itself, so entering it locked would starve exactly like the regression
    test_run_repair_does_not_hold_a_lock_that_blocks_its_own_writes pins for links."""
    from rebar._store import lock as _lock
    from rebar._store.gitutil import run_git as real_run_git

    tracker = wedged_store["tracker"]
    doctor = _patch_doctor(monkeypatch, tracker)
    tracker_s = str(tracker)

    lock_state: dict[str, bool] = {}

    def probing_run_git(cwd, *args, **kwargs):
        if args and args[0] == "checkout":
            lock_state["checkout"] = _lock.write_lock_is_busy(tracker_s)
        return real_run_git(cwd, *args, **kwargs)

    real_quarantine = doctor._quarantine_leftovers

    def probing_quarantine(t, paths):
        lock_state["quarantine"] = _lock.write_lock_is_busy(tracker_s)
        return real_quarantine(t, paths)

    def probing_reconverge(_tracker):
        lock_state["reconverge"] = _lock.write_lock_is_busy(tracker_s)

    monkeypatch.setattr(doctor, "run_git", probing_run_git)
    monkeypatch.setattr(doctor, "_quarantine_leftovers", probing_quarantine)
    monkeypatch.setattr(doctor, "_reconverge", probing_reconverge)

    findings = doctor.scan_dirty(tracker_s)
    doctor.run_repair(findings, tracker_s)

    assert lock_state == {"checkout": True, "quarantine": True, "reconverge": False}
    statuses = {f["kind"]: f.get("repair_status") for f in findings}
    assert statuses["tracker-dirty-deletion"] == "repaired"
    assert statuses["tracker-dirty-leftover"] == "repaired"
    assert statuses["tracker-dirty-tmp-event"] == "manual"


@pytest.mark.unit
def test_unclassified_stray_retired_is_left_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A .retired whose source is preserved nowhere committed is outside every class:
    doctor --repair must not move it (it may be the only copy of an event)."""
    tracker = _clean_clone(tmp_path)
    stray = "ffff-nowh-6666-6666/1700000000000000007-nowhere.json.retired"
    _write(tracker, stray, '{"only": "copy"}')
    doctor = _patch_doctor(monkeypatch, tracker)

    doctor.doctor_cli(["--repair"])
    capsys.readouterr()

    assert (tracker / stray).read_text(encoding="utf-8") == '{"only": "copy"}'
    assert _git(tracker, "for-each-ref", "refs/rebar-doctor/").stdout == ""


@pytest.mark.unit
def test_no_backup_ref_means_no_mutation(
    wedged_store: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The 'no backup ref, no mutation' safety contract: when the backup ref cannot be
    recorded, every mutating finding is refused as unrepairable, no file is touched,
    and reconverge is never entered — the run fails so the wedge stays visible."""
    tracker = wedged_store["tracker"]
    doctor = _patch_doctor(monkeypatch, tracker)
    reconverged: list[bool] = []
    monkeypatch.setattr(doctor, "_reconverge", lambda *_a: reconverged.append(True))
    monkeypatch.setattr(doctor, "_dirty_backup_ref", lambda _t: None)
    before = _git(tracker, "status", "--porcelain", "-uall").stdout

    rc = doctor.doctor_cli(["--repair", "--output", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    by_kind = {f["kind"]: f for f in payload["findings"]}
    for kind in ("tracker-dirty-deletion", "tracker-dirty-leftover"):
        assert by_kind[kind]["repair_status"] == "unrepairable"
        assert "backup ref" in by_kind[kind]["repair_reason"]
        assert "backup_ref" not in by_kind[kind]
    assert _git(tracker, "status", "--porcelain", "-uall").stdout == before
    assert reconverged == []


@pytest.mark.unit
def test_failed_mutations_are_unrepairable_and_fail_the_run(
    wedged_store: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed ``git checkout HEAD --`` and a failed quarantine move each mark their
    finding unrepairable (with the failure reason) and the run exits 1 — a repair that
    could not happen must never read as a heal."""
    from rebar._store.gitutil import run_git as real_run_git

    tracker = wedged_store["tracker"]
    doctor = _patch_doctor(monkeypatch, tracker)
    monkeypatch.setattr(doctor, "_reconverge", lambda *_a: None)
    monkeypatch.setattr(doctor, "_quarantine_leftovers", lambda *_a: False)

    def failing_checkout(cwd, *args, **kwargs):
        if args and args[0] == "checkout":
            return subprocess.CompletedProcess(
                args, returncode=1, stdout="", stderr="simulated checkout failure"
            )
        return real_run_git(cwd, *args, **kwargs)

    monkeypatch.setattr(doctor, "run_git", failing_checkout)

    rc = doctor.doctor_cli(["--repair", "--output", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    by_kind = {f["kind"]: f for f in payload["findings"]}
    deletion = by_kind["tracker-dirty-deletion"]
    assert deletion["repair_status"] == "unrepairable"
    assert deletion["repair_reason"] == "simulated checkout failure"
    leftover = by_kind["tracker-dirty-leftover"]
    assert leftover["repair_status"] == "unrepairable"
    assert "quarantine move failed" in leftover["repair_reason"]
    # The backup ref preceded the (attempted) mutations, as the protocol requires.
    assert _git(tracker, "for-each-ref", "refs/rebar-doctor/").stdout != ""


@pytest.mark.unit
def test_paths_with_spaces_survive_classifier_and_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """git status C-quotes unusual paths in porcelain v1; the classifier reads the
    NUL-terminated form so a path with spaces (or quotes) arrives verbatim, and the
    shlex-encoded fsck line carries it through the text→JSON round-trip intact."""
    from rebar._commands import fsck
    from rebar._commands.fsck_tracker_health import dirty_tracker_classes

    tracker = _clean_clone(tmp_path)
    spaced_del = "aaaa-tick-1111-1111/odd name.json"
    _write(tracker, spaced_del, "{}")
    _commit_all(tracker, "spaced event")
    (tracker / spaced_del).unlink()
    spaced_snap = "cccc-fold-3333-3333/weird name-SNAPSHOT.json"
    _write(tracker, spaced_snap, "{}")

    classes = dirty_tracker_classes(str(tracker))
    assert classes["deletions"] == [spaced_del]
    assert classes["leftovers"] == [spaced_snap]

    monkeypatch.setattr("rebar.config.tracker_dir", lambda _repo_root=None: tracker)
    fsck.fsck_cli(["--output", "json"], no_mutate=True)
    payload = json.loads(capsys.readouterr().out)
    by_kind = {i["kind"]: i for i in payload["issues"] if i["kind"].startswith("tracker_dirty")}
    assert by_kind["tracker_dirty_deletion"]["paths"] == [spaced_del]
    assert by_kind["tracker_dirty_leftover"]["paths"] == [spaced_snap]
