"""``doctor`` lock health — held/free, holder liveness, hold age, staleness.

The properties worth pinning are the ones an operator's decision hangs on. A HELD lock
with a live holder must read as information and must NOT fail the exit code, or every CI
gate keyed on ``rebar doctor`` turns red whenever two agents write concurrently. A lock
with no live owner must read as a finding, or the surface fails at the only job it was
added for (ticket metaphoric-fleeting-nutcracker). And the whole pass must be read-only:
a diagnostic that reclaims a lock it merely meant to describe would be a far worse bug
than the blind spot it replaced.

The staleness verdict is deliberately NOT re-derived here — the tests drive
``doctor_locks`` and assert the answers ``lock_owner._mkdir_lock_is_stale`` gives, so a
future change to that decision table shows up here rather than being shadowed by a second
copy of the rule.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import ModuleType

import pytest

from rebar._commands import doctor, doctor_locks
from rebar._store import lock as _lock
from rebar._store import lock_owner as _owner


def _tracker(tmp_path: Path) -> Path:
    """A store-shaped tracker: ``<repo>/.tickets-tracker``, so ``.rebar`` is its sibling."""
    tracker = tmp_path / "repo" / ".tickets-tracker"
    tracker.mkdir(parents=True)
    return tracker


def _by_name(reports: list[dict], name: str) -> dict:
    match = [r for r in reports if r["name"] == name]
    assert match, (name, [r["name"] for r in reports])
    return match[0]


def _stamp(lock_dir: Path, text: str) -> None:
    lock_dir.mkdir(exist_ok=True)
    (lock_dir / _owner._MKDIR_OWNER_FILE).write_text(text, encoding="utf-8")


def _dead_pid() -> int:
    """A pid that is not running: fork-free, by claiming one far above the live range.

    ``os.kill(pid, 0)`` on an unallocated pid raises ``ProcessLookupError``, which is
    exactly the "not-running" signal the staleness table keys on.
    """
    pid = 4_194_303  # above the default pid_max on every platform in the matrix
    while _owner._pid_alive(pid):  # pragma: no cover - practically never taken
        pid -= 1
    return pid


# ---------------------------------------------------------------------------
# free store
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_a_free_store_reports_every_leg_and_no_finding(tmp_path: Path) -> None:
    """All five legs are reported even when nothing is held — "the lock is free" is the
    answer an operator most often needs, and a missing row is indistinguishable from a
    check that never ran."""
    tracker = _tracker(tmp_path)

    reports = doctor_locks.scan_locks(str(tracker))

    assert {r["name"] for r in reports} == {
        doctor_locks.LEG_TICKETS_FCNTL,
        doctor_locks.LEG_TICKETS_MKDIR,
        doctor_locks.LEG_HLC,
        doctor_locks.LEG_ENRICH_DRAIN,
        doctor_locks.LEG_COMPACT_WORKER,
    }
    assert _by_name(reports, doctor_locks.LEG_TICKETS_MKDIR)["state"] == "free"
    compact = _by_name(reports, doctor_locks.LEG_COMPACT_WORKER)
    assert compact["state"] == "free"
    assert compact["holder"] is None, "a free store must not report a spurious holder"
    # The fcntl files are created lazily by the first acquirer, so a never-written store
    # reports them absent rather than inventing them: probing must not create a lock file.
    assert _by_name(reports, doctor_locks.LEG_TICKETS_FCNTL)["state"] == "absent"
    assert not (tracker / _lock.WRITE_LOCK_NAME).exists(), "the probe created a lock file"
    assert doctor_locks.lock_findings(reports) == []


# ---------------------------------------------------------------------------
# held and healthy
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_a_live_holder_is_reported_but_is_not_a_finding(tmp_path: Path) -> None:
    """A really-held lock names its holder, ages it, calls the pid live — and produces
    NO finding. Contention is normal operation."""
    tracker = _tracker(tmp_path)

    handle = _lock.acquire(str(tracker), timeout=5, attempts=1)
    try:
        reports = doctor_locks.scan_locks(str(tracker))
    finally:
        handle.release()

    mkdir = _by_name(reports, doctor_locks.LEG_TICKETS_MKDIR)
    assert mkdir["state"] == "held"
    assert mkdir["holder"]["pid"] == str(os.getpid())
    assert mkdir["holder"]["host"] == _owner._host_identity()
    assert set(mkdir["holder"]) == {"host", "ns", "pid", "start"}
    assert mkdir["pid_state"] == "live"
    assert mkdir["held_seconds"] is not None and mkdir["held_seconds"] >= 0
    assert mkdir["staleness"] == doctor_locks.STALENESS_NOT_STALE
    # The fcntl leg is held by THIS process, and flock is per-process: re-probing our own
    # lock succeeds, so the honest assertion is that the leg exists and was answered.
    assert _by_name(reports, doctor_locks.LEG_TICKETS_FCNTL)["state"] in {"held", "free"}
    assert doctor_locks.lock_findings(reports) == []


@pytest.mark.unit
@pytest.mark.scripts
def test_a_contended_fcntl_leg_reads_as_held(tmp_path: Path) -> None:
    """Held/free for the kernel leg comes from the same non-blocking probe ``acquire``
    uses, so a leg held by ANOTHER process reads as held rather than free."""
    import multiprocessing

    tracker = _tracker(tmp_path)
    lock_path = tracker / _lock.WRITE_LOCK_NAME
    ready = multiprocessing.Event()
    finish = multiprocessing.Event()
    holder = multiprocessing.Process(
        target=_hold_fcntl, args=(str(lock_path), ready, finish), daemon=True
    )
    holder.start()
    try:
        assert ready.wait(30), "the holder process never took the lock"
        state = doctor_locks._probe_fcntl(str(lock_path))
    finally:
        finish.set()
        holder.join(30)

    assert state == "held", state
    # A probe must never leave the lock taken: with the holder gone it reads free again.
    assert doctor_locks._probe_fcntl(str(lock_path)) == "free"


def _hold_fcntl(path: str, ready, finish) -> None:  # pragma: no cover - child process
    import fcntl as _fcntl

    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    _fcntl.flock(fd, _fcntl.LOCK_EX)
    ready.set()
    finish.wait(60)
    os.close(fd)


# ---------------------------------------------------------------------------
# staleness — delegated, never re-derived
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_a_dead_holder_is_a_finding(tmp_path: Path) -> None:
    """A same-host stamp naming a pid that is not running is what
    ``_mkdir_lock_is_stale`` calls reclaimable, so doctor reports it as a finding."""
    tracker = _tracker(tmp_path)
    lock_dir = tracker / _lock.MKDIR_LOCK_NAME
    dead = _dead_pid()
    _stamp(
        lock_dir,
        f"rebar-lock v2 host={_owner._host_identity()} "
        f"ns={_owner._read_pid_namespace_id() or '-'} pid={dead} start=-",
    )

    reports = doctor_locks.scan_locks(str(tracker))
    mkdir = _by_name(reports, doctor_locks.LEG_TICKETS_MKDIR)
    findings = doctor_locks.lock_findings(reports)

    assert mkdir["state"] == "held"
    assert mkdir["pid_state"] == "not-running"
    assert mkdir["staleness"] == doctor_locks.STALENESS_STALE
    assert [f["kind"] for f in findings] == [doctor_locks.KIND_STALE_LOCK]
    assert findings[0]["lock"] == doctor_locks.LEG_TICKETS_MKDIR
    assert "reclaims it automatically" in findings[0]["advice"]


@pytest.mark.unit
@pytest.mark.scripts
def test_a_foreign_host_holder_is_never_probed(tmp_path: Path) -> None:
    """A stamp from another host identity yields ``unprobeable``, never a pid verdict:
    that pid number names a different process (or nothing) on our kernel."""
    tracker = _tracker(tmp_path)
    lock_dir = tracker / _lock.MKDIR_LOCK_NAME
    _stamp(lock_dir, "rebar-lock v2 host=boot-somewhere-else ns=1 pid=1 start=1")

    mkdir = _by_name(doctor_locks.scan_locks(str(tracker)), doctor_locks.LEG_TICKETS_MKDIR)

    assert mkdir["pid_state"] == "unprobeable (foreign host)"
    assert mkdir["holder"]["host"] == "boot-somewhere-else"
    # Refusal-without-proof: a fresh foreign lock is honoured, not called stale.
    assert mkdir["staleness"] == doctor_locks.STALENESS_NOT_STALE


@pytest.mark.unit
@pytest.mark.scripts
def test_an_unreadable_stamp_is_stated_not_guessed(tmp_path: Path) -> None:
    """A lock dir with no owner file (the window between mkdir and the stamp write, or a
    bash-era lock) is described explicitly rather than half-rendered."""
    tracker = _tracker(tmp_path)
    (tracker / _lock.MKDIR_LOCK_NAME).mkdir()

    mkdir = _by_name(doctor_locks.scan_locks(str(tracker)), doctor_locks.LEG_TICKETS_MKDIR)

    assert mkdir["state"] == "held"
    assert mkdir["holder"] is None
    assert mkdir["holder_description"] == "unknown (no ownership stamp)"
    assert mkdir["pid_state"] is None


@pytest.mark.unit
@pytest.mark.scripts
def test_an_unrecognised_stamp_is_stated_not_guessed(tmp_path: Path) -> None:
    """A shape ``_parse_v2_stamp`` declines (a NEWER rebar's stamp) is reported as
    unrecognised — the forward-compatibility path, not a parse attempt."""
    tracker = _tracker(tmp_path)
    _stamp(tracker / _lock.MKDIR_LOCK_NAME, "rebar-lock v9 something=else")

    mkdir = _by_name(doctor_locks.scan_locks(str(tracker)), doctor_locks.LEG_TICKETS_MKDIR)

    assert mkdir["holder_description"] == "unknown (unrecognised ownership stamp)"


# ---------------------------------------------------------------------------
# the .rebar legs
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_a_missing_rebar_dir_reports_absent_rather_than_raising(tmp_path: Path) -> None:
    """A store that has never enriched or stamped a clock has no ``.rebar`` dir; the two
    legs living there must degrade to a row, not abort the run."""
    tracker = _tracker(tmp_path)
    assert not (tracker.parent / ".rebar").exists()

    reports = doctor_locks.scan_locks(str(tracker))

    assert _by_name(reports, doctor_locks.LEG_HLC)["state"] == "absent"
    assert _by_name(reports, doctor_locks.LEG_ENRICH_DRAIN)["state"] == "free"


def _plant_drain_lock(tracker: Path, stamp: str, *, age_s: float = 0.0) -> Path:
    rebar_dir = tracker.parent / ".rebar"
    rebar_dir.mkdir(exist_ok=True)
    path = rebar_dir / "enrich-drain.lock"
    path.write_text(stamp, encoding="utf-8")
    if age_s:
        when = time.time() - age_s
        os.utime(path, (when, when))
    return path


def _drain_stamp(**overrides: str) -> str:
    fields = dict(
        token.split("=", 1) for token in _owner._owner_stamp().split()[2:] if "=" in token
    )
    fields.update(overrides)
    return "rebar-lock v2 " + " ".join(f"{k}={v}" for k, v in fields.items())


@pytest.mark.unit
@pytest.mark.scripts
def test_the_drain_lock_names_a_live_holder_and_is_not_a_finding(tmp_path: Path) -> None:
    """The drain lock now carries the v2 stamp (bug knavish-stimulated-bluebottle), so its
    row names a holder and carries a REAL staleness verdict instead of ``not-assessable``.
    A live holder is information, never a finding."""
    tracker = _tracker(tmp_path)
    _plant_drain_lock(tracker, _drain_stamp())  # this live process

    drain = _by_name(doctor_locks.scan_locks(str(tracker)), doctor_locks.LEG_ENRICH_DRAIN)

    assert drain["state"] == "held"
    assert drain["held_seconds"] is not None
    assert drain["holder"]["pid"] == str(os.getpid())
    assert drain["pid_state"] == "live"
    assert drain["staleness"] == doctor_locks.STALENESS_NOT_STALE
    assert doctor_locks.lock_findings([drain]) == []


@pytest.mark.unit
@pytest.mark.scripts
def test_a_dead_holder_drain_lock_is_a_stale_finding(tmp_path: Path) -> None:
    """The wedge the bug describes: a drainer that died without releasing."""
    tracker = _tracker(tmp_path)
    _plant_drain_lock(tracker, _drain_stamp(pid=str(_dead_pid())))

    drain = _by_name(doctor_locks.scan_locks(str(tracker)), doctor_locks.LEG_ENRICH_DRAIN)

    assert drain["staleness"] == doctor_locks.STALENESS_STALE
    findings = doctor_locks.lock_findings([drain])
    assert [f["kind"] for f in findings] == [doctor_locks.KIND_STALE_LOCK]


@pytest.mark.unit
@pytest.mark.scripts
def test_an_unstamped_drain_lock_is_adjudicated_by_the_shared_ceiling(tmp_path: Path) -> None:
    """A lock written by a rebar predating the stamp: unattributable, so the shared
    wall-clock ceiling decides — young is honoured, aged-out is stale."""
    tracker = _tracker(tmp_path)

    _plant_drain_lock(tracker, "")
    fresh = _by_name(doctor_locks.scan_locks(str(tracker)), doctor_locks.LEG_ENRICH_DRAIN)
    assert fresh["holder_description"] == "unknown (no ownership stamp)"
    assert fresh["staleness"] == doctor_locks.STALENESS_NOT_STALE

    _plant_drain_lock(tracker, "", age_s=_owner._MKDIR_LOCK_STALE_CEILING_S + 60)
    aged = _by_name(doctor_locks.scan_locks(str(tracker)), doctor_locks.LEG_ENRICH_DRAIN)
    assert aged["staleness"] == doctor_locks.STALENESS_STALE


@pytest.mark.unit
@pytest.mark.scripts
def test_an_unrecognised_drain_stamp_is_stated_not_guessed(tmp_path: Path) -> None:
    """A drain lock whose contents ``_parse_v2_stamp`` declines outright (a NEWER rebar's
    dialect) is reported as unrecognised, with no invented holder: the forward-compatible
    refusal, not a half-parse. Young, so the shared ceiling still honours it."""
    tracker = _tracker(tmp_path)
    _plant_drain_lock(tracker, "rebar-lock v9 something=else")

    drain = _by_name(doctor_locks.scan_locks(str(tracker)), doctor_locks.LEG_ENRICH_DRAIN)

    assert drain["holder_description"] == "unknown (unrecognised ownership stamp)"
    assert drain["holder"] is None
    assert drain["pid_state"] is None
    assert drain["staleness"] == doctor_locks.STALENESS_NOT_STALE


@pytest.mark.unit
@pytest.mark.scripts
def test_an_incomplete_drain_stamp_is_stated_not_guessed(tmp_path: Path) -> None:
    """A v2 stamp missing required fields — a torn mid-write read — is reported as
    INCOMPLETE rather than unrecognised or absent, so an operator can tell a drainer
    caught between create and stamp apart from a lock that was never stamped."""
    tracker = _tracker(tmp_path)
    _plant_drain_lock(tracker, "rebar-lock v2 host=boot-x ns=1")

    drain = _by_name(doctor_locks.scan_locks(str(tracker)), doctor_locks.LEG_ENRICH_DRAIN)

    assert drain["holder_description"] == "unknown (incomplete ownership stamp)"
    assert drain["holder"] is None
    assert drain["pid_state"] is None
    assert drain["staleness"] == doctor_locks.STALENESS_NOT_STALE


# ---------------------------------------------------------------------------
# the compact-worker lock (existence + v2 stamp, exactly the drain lock's shape)
# ---------------------------------------------------------------------------


def _plant_compact_lock(tracker: Path, stamp: str, *, age_s: float = 0.0) -> Path:
    rebar_dir = tracker.parent / ".rebar"
    rebar_dir.mkdir(exist_ok=True)
    path = rebar_dir / "compact-worker.lock"
    path.write_text(stamp, encoding="utf-8")
    if age_s:
        when = time.time() - age_s
        os.utime(path, (when, when))
    return path


@pytest.mark.unit
@pytest.mark.scripts
def test_the_compact_lock_names_a_live_holder_and_is_not_a_finding(tmp_path: Path) -> None:
    """The compaction trigger's worker lock (``compact_trigger``, which reuses the drain's
    stamped-lock machinery) joins the census: a live detached compactor is information —
    named, aged, called live — never a finding."""
    tracker = _tracker(tmp_path)
    _plant_compact_lock(tracker, _drain_stamp())  # this live process

    report = _by_name(doctor_locks.scan_locks(str(tracker)), doctor_locks.LEG_COMPACT_WORKER)

    assert report["state"] == "held"
    assert report["held_seconds"] is not None
    assert report["holder"]["pid"] == str(os.getpid())
    assert report["pid_state"] == "live"
    assert report["staleness"] == doctor_locks.STALENESS_NOT_STALE
    assert doctor_locks.lock_findings([report]) == []


@pytest.mark.unit
@pytest.mark.scripts
def test_a_dead_holder_compact_lock_is_a_stale_finding(tmp_path: Path) -> None:
    """The gap this leg exists for: a detached compaction worker that died between
    acquire and release leaves a stamped lock doctor could not previously see."""
    tracker = _tracker(tmp_path)
    _plant_compact_lock(tracker, _drain_stamp(pid=str(_dead_pid())))

    report = _by_name(doctor_locks.scan_locks(str(tracker)), doctor_locks.LEG_COMPACT_WORKER)

    assert report["staleness"] == doctor_locks.STALENESS_STALE
    findings = doctor_locks.lock_findings([report])
    assert [f["kind"] for f in findings] == [doctor_locks.KIND_STALE_LOCK]


# ---------------------------------------------------------------------------
# doctor integration: exit code, JSON contract, read-only guarantee
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_doctor_json_carries_the_lock_report_and_stays_green_on_a_live_holder(
    graph: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    """A clean store whose lock is HELD by a live process still exits 0, and the JSON
    envelope carries the documented lock keys."""
    tracker = _tracker(tmp_path)
    monkeypatch.setattr(doctor, "tracker_dir", lambda _repo_root=None: tracker)

    handle = _lock.acquire(str(tracker), timeout=5, attempts=1)
    try:
        rc = doctor.doctor_cli(["--output", "json"])
    finally:
        handle.release()
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0, "a live holder is information, not a finding"
    assert set(payload) >= {"findings", "finding_count", "locks", "lock_findings"}
    assert payload["lock_findings"] == []
    mkdir = _by_name(payload["locks"], doctor_locks.LEG_TICKETS_MKDIR)
    assert mkdir["state"] == "held"
    assert mkdir["pid_state"] == "live"


@pytest.mark.unit
@pytest.mark.scripts
def test_doctor_exits_one_on_a_stale_lock(
    graph: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    """A dead-holder lock is outstanding by doctor's existing rule, so the command
    exits 1 and the text output names it."""
    tracker = _tracker(tmp_path)
    _stamp(
        tracker / _lock.MKDIR_LOCK_NAME,
        f"rebar-lock v2 host={_owner._host_identity()} "
        f"ns={_owner._read_pid_namespace_id() or '-'} pid={_dead_pid()} start=-",
    )
    monkeypatch.setattr(doctor, "tracker_dir", lambda _repo_root=None: tracker)

    rc = doctor.doctor_cli([])
    out = capsys.readouterr().out

    assert rc == 1
    assert "stale-lock" in out
    assert doctor_locks.LEG_TICKETS_MKDIR in out
    assert "doctor: locks" in out
    assert "doctor: 1 stale lock(s)" in out


@pytest.mark.unit
@pytest.mark.scripts
def test_doctor_never_touches_a_lock(
    graph: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    """The load-bearing guarantee: a doctor pass over a stale lock leaves the lock dir,
    its owner file and the drain lock byte-identical — including with ``--repair``, whose
    repair loop must never reach a lock."""
    tracker = _tracker(tmp_path)
    lock_dir = tracker / _lock.MKDIR_LOCK_NAME
    stamp = (
        f"rebar-lock v2 host={_owner._host_identity()} "
        f"ns={_owner._read_pid_namespace_id() or '-'} pid={_dead_pid()} start=-"
    )
    _stamp(lock_dir, stamp)
    rebar_dir = tracker.parent / ".rebar"
    rebar_dir.mkdir()
    drain = rebar_dir / "enrich-drain.lock"
    drain.write_text("claimant", encoding="utf-8")
    monkeypatch.setattr(doctor, "tracker_dir", lambda _repo_root=None: tracker)
    monkeypatch.setattr(doctor, "_reconciler_in_flight", lambda *_a, **_k: False)

    doctor.doctor_cli([])
    doctor.doctor_cli(["--repair"])
    capsys.readouterr()

    assert lock_dir.is_dir(), "doctor removed the lock directory"
    assert (lock_dir / _owner._MKDIR_OWNER_FILE).read_text(encoding="utf-8") == stamp
    assert drain.read_text(encoding="utf-8") == "claimant"
