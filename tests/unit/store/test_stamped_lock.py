"""One stamped-file advisory lock, shared by the three workers (story 1cf6-f902-5bfa-438f).

The "``O_EXCL``-create a lock file, write a v2 ownership stamp, reclaim ONCE if the shared
staleness table condemns the holder" loop existed **three** times — ``llm/enrich_drain.py``,
``_commands/compact_trigger.py`` and ``_snapshot/gc_trigger.py``. Copying it is what spread the
unresolved-path defect (``da68-fc7c-068c-4c53``) and what let the three dialects drift apart
(``aadc-9af6-0e67-4e2a``).

These tests pin the consolidation the way ``tests/unit/store/test_store_paths.py`` pins the
store-path one: the behaviour is correct, AND the construct exists in exactly one place, so a
fourth copy cannot re-enter by imitation.

The drift the merge had to ADJUDICATE rather than average was the release path: the drain put
``os.close`` and ``os.unlink`` in ONE ``try``, so a failing close SKIPPED the unlink and leaked
the lock until the 3600 s ceiling; the other two used two independent ``try`` blocks. The
shared implementation takes the two-block form — a deliberate fix to the drain, pinned by
``test_release_unlinks_even_when_close_fails``.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from _tree_scan import parsed_python_files

import rebar

#: Anchored on the PACKAGE, not on the module under construction: the uniqueness guard below
#: must run — and fail honestly — even when ``_store/stamped_lock.py`` does not import, because
#: its subject is the rest of the tree, not the owner.
_SRC_REBAR = Path(rebar.__file__).resolve().parent
_OWNER = _SRC_REBAR / "_store" / "stamped_lock.py"


def _lock_path(tmp_path: Path) -> str:
    return str(tmp_path / "worker.lock")


def _dead_pid() -> int:
    """A pid that is provably not running: a child we started, waited for, and reaped."""
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


def _stamp_with(**overrides: str) -> str:
    """Our own v2 stamp with individual fields substituted."""
    from rebar._store import lock_owner as owner

    fields = dict(token.split("=", 1) for token in owner._owner_stamp().split()[2:] if "=" in token)
    fields.update(overrides)
    return "rebar-lock v2 " + " ".join(f"{k}={v}" for k, v in fields.items())


def _plant(path: str, stamp: str, *, age_s: float = 0.0) -> str:
    """A lock file already on disk, as a crashed worker would have left it."""
    import time

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(stamp)
    if age_s:
        when = time.time() - age_s
        os.utime(path, (when, when))
    return path


# ======================================================================================
# HAPPY PATH
# ======================================================================================
def test_acquire_stamps_the_file_and_release_removes_it(tmp_path: Path) -> None:
    """The whole cycle, asserted on the artifact: the file appears carrying THIS process's
    v2 stamp, and release takes it away so the next worker can have it."""
    from rebar._store import lock_owner as owner
    from rebar._store.stamped_lock import release_stamped_lock, stamped_file_lock

    path = _lock_path(tmp_path)
    fd = stamped_file_lock(path, label="test worker")

    assert fd is not None
    assert os.path.exists(path)
    fields = owner._parse_v2_stamp(Path(path).read_text(encoding="utf-8").strip())
    assert fields, "the acquired lock must carry a parseable v2 stamp"
    assert fields["host"] == owner._host_identity()
    assert fields["pid"] == str(os.getpid())

    release_stamped_lock(path, fd)
    assert not os.path.exists(path)

    again = stamped_file_lock(path, label="test worker")
    assert again is not None, "a released lock must be re-acquirable"
    release_stamped_lock(path, again)


def test_a_second_acquire_is_refused_while_the_first_is_held(tmp_path: Path) -> None:
    """Exclusion, proved by two REAL acquires rather than by inspecting the implementation."""
    from rebar._store.stamped_lock import release_stamped_lock, stamped_file_lock

    path = _lock_path(tmp_path)
    first = stamped_file_lock(path, label="test worker")
    assert first is not None

    assert stamped_file_lock(path, label="test worker") is None, (
        "a live holder's lock must be respected"
    )

    release_stamped_lock(path, first)
    assert stamped_file_lock(path, label="test worker") is not None


# ======================================================================================
# HELD OUT — edge / boundary / contrast
# ======================================================================================
def test_release_unlinks_even_when_close_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE behaviour delta the consolidation adjudicates. The drain wrapped close+unlink in one
    ``try``, so an ``OSError`` from ``os.close`` skipped the unlink and leaked the lock until
    the 3600 s ceiling — every drain in that hour silently skipped. Two independent ``try``
    blocks: the unlink always runs."""
    from rebar._store.stamped_lock import release_stamped_lock, stamped_file_lock

    path = _lock_path(tmp_path)
    fd = stamped_file_lock(path, label="test worker")
    assert fd is not None and os.path.exists(path)

    real_close = os.close

    def _refusing_close(target_fd: int) -> None:
        if target_fd == fd:
            real_close(target_fd)  # really close it; the caller must not leak the descriptor
            raise OSError("close failed after flush")
        real_close(target_fd)

    monkeypatch.setattr(os, "close", _refusing_close)
    release_stamped_lock(path, fd)
    monkeypatch.setattr(os, "close", real_close)

    assert not os.path.exists(path), (
        "a failing close must not skip the unlink — the lock would leak until the ceiling"
    )
    assert stamped_file_lock(path, label="test worker") is not None


def test_a_provably_dead_holder_is_reclaimed_loudly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A worker that died between acquire and release must not wedge its trigger forever."""
    from rebar._store.stamped_lock import stamped_file_lock

    path = _plant(_lock_path(tmp_path), _stamp_with(pid=str(_dead_pid())))

    with caplog.at_level(logging.WARNING):
        fd = stamped_file_lock(path, label="test worker")

    assert fd is not None, "a provably-orphaned lock must be reclaimed, not respected"
    assert any("reclaiming stale" in r.getMessage() for r in caplog.records)


def test_a_live_holder_is_never_reclaimed(tmp_path: Path) -> None:
    """The negative control that makes the reclaim test meaningful: our own live stamp is
    refused, and the planted bytes are neither broken nor rewritten."""
    from rebar._store.stamped_lock import stamped_file_lock

    path = _plant(_lock_path(tmp_path), _stamp_with())
    before = Path(path).read_bytes()

    assert stamped_file_lock(path, label="test worker") is None
    assert Path(path).read_bytes() == before


def test_a_fresh_unstamped_lock_is_respected_but_an_aged_one_is_reclaimed(tmp_path: Path) -> None:
    """The create/stamp window versus the pre-stamp orphan: the inherited wall-clock ceiling
    (``lock_owner._MKDIR_LOCK_STALE_CEILING_S``) is what separates them, and the shared helper
    must keep DELEGATING that call rather than growing staleness logic of its own."""
    from rebar._store import lock_owner as owner
    from rebar._store.stamped_lock import stamped_file_lock

    path = _plant(_lock_path(tmp_path), "")
    assert stamped_file_lock(path, label="test worker") is None, "a fresh window is respected"

    _plant(path, "", age_s=owner._MKDIR_LOCK_STALE_CEILING_S + 60)
    fd = stamped_file_lock(path, label="test worker")
    assert fd is not None, "an aged-out unstamped orphan must be reclaimable"


def test_the_reclaim_retries_exactly_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bounded: a lock that keeps reappearing must give up rather than spin, so a third
    collision returns ``None`` after exactly two create attempts."""
    from rebar._store import lock_owner as owner
    from rebar._store.stamped_lock import stamped_file_lock

    path = _plant(_lock_path(tmp_path), "", age_s=owner._MKDIR_LOCK_STALE_CEILING_S + 60)
    real_open = os.open
    attempts: list[str] = []

    def _always_taken(target: object, *args: object, **kwargs: object) -> int:
        if str(target) == path:
            attempts.append(str(target))
            raise FileExistsError(target)
        return real_open(target, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", _always_taken)
    assert stamped_file_lock(path, label="test worker") is None
    assert len(attempts) == 2, "the original create + exactly one post-reclaim retry"


def test_acquire_swallows_an_unexpected_open_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """These are BEST-EFFORT worker locks: an open failure that is not a collision must
    degrade to "no lock" rather than raise into a drain, a close, or a GC trigger."""
    from rebar._store.stamped_lock import stamped_file_lock

    def _boom(*_args: object, **_kwargs: object) -> int:
        raise OSError("read-only file system")

    monkeypatch.setattr(os, "open", _boom)
    assert stamped_file_lock(_lock_path(tmp_path), label="test worker") is None


def test_a_failed_stamp_write_still_returns_the_fd_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A full disk must not turn into a failed acquire. The fd is still returned, the loss of
    attribution is ANNOUNCED (the drain's WARNING, adopted for all three — the other two used
    to swallow it), and the unstamped lock stays bounded by the shared ceiling."""
    from rebar._store.stamped_lock import stamped_file_lock

    path = _lock_path(tmp_path)
    real_write = os.write
    opened: set[int] = set()
    real_open = os.open

    def _tracking_open(target: object, *args: object, **kwargs: object) -> int:
        fd = real_open(target, *args, **kwargs)  # type: ignore[arg-type]
        if str(target) == path:
            opened.add(fd)
        return fd

    def _refusing_write(fd: int, data: bytes) -> int:
        if fd in opened:
            raise OSError("no space left on device")
        return real_write(fd, data)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "open", _tracking_open)
        mp.setattr(os, "write", _refusing_write)
        with caplog.at_level(logging.WARNING):
            fd = stamped_file_lock(path, label="test worker")

    assert fd is not None, "a stamp write failure must not turn into a failed acquire"
    assert any("lock is unattributable" in r.getMessage() for r in caplog.records)
    assert Path(path).read_text(encoding="utf-8") == ""
    os.close(fd)


def test_the_reclaim_warning_names_the_holder_when_a_describer_is_given(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The drain's operator affordance, kept: it passes ``_describe_drain_lock_holder`` so a
    wedge is attributable without running ``rebar doctor``. The other two pass ``None`` and
    must NOT gain a phantom holder clause."""
    from rebar._store.stamped_lock import stamped_file_lock

    path = _plant(_lock_path(tmp_path), _stamp_with(pid=str(_dead_pid())))
    with caplog.at_level(logging.WARNING):
        stamped_file_lock(path, label="test worker", describe_holder=lambda _p: "a named holder")
    assert any("held by a named holder" in r.getMessage() for r in caplog.records)

    caplog.clear()
    _plant(path, _stamp_with(pid=str(_dead_pid())))
    with caplog.at_level(logging.WARNING):
        stamped_file_lock(path, label="test worker")
    assert any("reclaiming stale" in r.getMessage() for r in caplog.records)
    assert not any("held by" in r.getMessage() for r in caplog.records)


def test_the_helper_accepts_a_path_object(tmp_path: Path) -> None:
    """``gc_trigger`` derives a ``Path`` from ``_gc_dir`` while the other two carry ``str``;
    the shared helper takes an already-resolved path of either type and owns no directory
    creation of its own (the three callers create theirs three different ways)."""
    from rebar._store.stamped_lock import release_stamped_lock, stamped_file_lock

    path = tmp_path / "worker.lock"
    fd = stamped_file_lock(path, label="test worker")
    assert fd is not None and path.exists()
    release_stamped_lock(path, fd)
    assert not path.exists()


def test_the_stamp_is_strip_equal_and_doctor_resolves_a_holder_from_it(tmp_path: Path) -> None:
    """The stamp bytes drifted (the drain appended a newline, the other two did not) and BOTH
    readers ``.read().strip()``, so the choice is invisible — but only as long as it stays
    strip-equal to ``lock_owner._owner_stamp()``. Proved through doctor's real reader."""
    from rebar._commands import doctor_locks
    from rebar._store import lock_owner as owner
    from rebar._store.stamped_lock import stamped_file_lock

    path = _lock_path(tmp_path)
    fd = stamped_file_lock(path, label="test worker")
    assert fd is not None

    written = Path(path).read_text(encoding="utf-8")
    assert written.strip() == written, "the shared stamp carries no trailing newline"
    assert owner._parse_v2_stamp(written.strip()) == owner._parse_v2_stamp(owner._owner_stamp()), (
        "the shared stamp is not the shared owner stamp"
    )

    row = doctor_locks._existence_report("test-worker", path, note="held")
    assert row["state"] == doctor_locks.STATE_HELD
    assert row["holder"] is not None
    assert row["holder"]["pid"] == str(os.getpid())
    os.close(fd)


# ======================================================================================
# HELD OUT — the construct-uniqueness guard
# ======================================================================================
_MARKER_RE = re.compile(r"#\s*stamped-lock-ok:(.*)")


def _acquire_offenders() -> list[str]:
    """Every unsanctioned stamped-lock acquire under ``src/rebar`` outside the owner.

    The rule itself lives in ``rebar._store.stamped_lock._offending_source`` and is unit-tested
    directly below; re-implementing the matcher here would make this guard a second copy of the
    very construct it exists to keep singular. The anchor is the CONJUNCTION named in the plan
    — ``os.O_CREAT`` and ``stamped_file_is_stale`` — because a bare ``O_EXCL`` scan over-matches
    seven legitimate lines and still flags ``doctor_locks``, the diagnostic READER.
    """
    offenders: list[str] = []
    from rebar._store.stamped_lock import _offending_source

    for module in parsed_python_files(_SRC_REBAR):
        if module.path.resolve() == _OWNER.resolve():
            continue  # the owner is exempt: refactoring INSIDE it cannot fail the guard
        why = _offending_source(module.source)
        if why:
            offenders.append(f"{module.path.relative_to(_SRC_REBAR.parent)}: {why}")
    return offenders


def test_the_stamped_lock_acquire_appears_only_in_its_owner() -> None:
    """A STATIC scan, not a runtime assertion: a fourth copy-pasted acquire that no test
    happens to execute must still fail here. This is what makes the consolidation durable —
    the class (one loop, replicated by imitation, then drifting) cannot re-enter by copy-paste.
    """
    assert _acquire_offenders() == [], (
        "a stamped-file lock acquire leaked outside _store/stamped_lock.py — route it through "
        "rebar._store.stamped_lock.stamped_file_lock instead, or annotate the module with "
        "'# stamped-lock-ok: <reason>': " + repr(_acquire_offenders())
    )


def test_the_unrelated_o_excl_uses_survive() -> None:
    """The companion criterion: the guard must leave the seven legitimate ``O_EXCL`` lines
    alone — prose in three modules, a real unrelated create in ``keyprov``, and the literal
    trigger token in the code-review overlay — which is exactly why the anchor is the
    conjunction and not ``O_EXCL``."""
    survivors = {
        "opcert_service/keyprov.py": 1,
        "llm/code_review/registry.py": 1,
        "_signing_hmac.py": 2,
        "_store/fsutil.py": 1,
        "_commands/doctor_locks.py": 2,
    }
    for rel, count in survivors.items():
        text = (_SRC_REBAR / rel).read_text(encoding="utf-8")
        assert text.count("O_EXCL") == count, f"an unrelated O_EXCL use moved in {rel}"


def test_the_scan_flags_a_copied_acquire() -> None:
    """The guard's own unit test: the tree scan above proves "no offender exists TODAY", which
    passes just as well if the scanner is broken. This drives the classifier on a synthetic
    copy, so it is proven to FLAG as well as to pass."""
    from rebar._store.stamped_lock import _offending_source

    assert _offending_source(
        "fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)\n"
        "if _owner.stamped_file_is_stale(p):\n    os.unlink(p)\n"
    )


def test_the_scan_ignores_a_reader_and_an_unrelated_create() -> None:
    """The negative controls that make the guard usable: ``doctor_locks`` adjudicates staleness
    but deliberately opens WITHOUT ``O_CREAT``, and ``keyprov`` creates exclusively but has
    nothing to do with ownership stamps. Flagging either would get the guard turned off."""
    from rebar._store.stamped_lock import _offending_source

    assert (
        _offending_source("stale = _owner.stamped_file_is_stale(path)\nos.open(p, os.O_RDONLY)")
        is None
    )
    assert _offending_source("fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)") is None
    assert _offending_source("x = compute(a, b)  # unrelated\n") is None


def test_a_reasoned_marker_suppresses_the_offence() -> None:
    """A legitimate fourth acquire stays possible — but only VISIBLY, with its reason in
    review, the same escape hatch ``# store-path-ok:`` and ``# raw-git-ok:`` provide."""
    from rebar._store.stamped_lock import _offending_source

    assert (
        _offending_source(
            "# stamped-lock-ok: a second mechanism, reviewed on ticket X\n"
            "fd = os.open(p, os.O_CREAT | os.O_EXCL, 0o644)\n"
            "stamped_file_is_stale(p)\n"
        )
        is None
    )


def test_a_reason_less_marker_is_itself_an_offence() -> None:
    """A bare marker would let the exception hide, so it is a violation in its own right."""
    from rebar._store.stamped_lock import _offending_source

    got = _offending_source(
        "# stamped-lock-ok:\n"
        "fd = os.open(p, os.O_CREAT | os.O_EXCL, 0o644)\nstamped_file_is_stale(p)\n"
    )
    assert got is not None and "requires a reason" in got


# ======================================================================================
# HELD OUT — end to end through the three REAL workers
# ======================================================================================
def _tracker(tmp_path: Path, name: str) -> str:
    tracker = Path(os.path.realpath(tmp_path)) / name / ".tickets-tracker"
    tracker.mkdir(parents=True)
    return str(tracker)


def test_every_worker_still_excludes_a_second_holder_and_releases(tmp_path: Path) -> None:
    """The teeth. Testing the helper alone would pass even if no worker were rewired, so this
    drives all SIX production functions: acquire, prove a second acquire is refused, release,
    prove the file is gone and the lock re-acquirable. The exclusion is what every one of these
    triggers exists for, and it is asserted on real acquires, not on internals."""
    from rebar._commands import compact_trigger
    from rebar._snapshot import gc_trigger
    from rebar.llm import enrich_drain

    gc_root = Path(os.path.realpath(tmp_path)) / "store"
    gc_root.mkdir()
    workers = [
        (
            "enrich drain",
            enrich_drain._acquire_advisory_lock,
            enrich_drain._release_advisory_lock,
            _tracker(tmp_path, "drain-repo"),
            enrich_drain._drain_lock_path,
        ),
        (
            "compaction trigger",
            compact_trigger._acquire_trigger_lock,
            compact_trigger.release_trigger_lock,
            _tracker(tmp_path, "compact-repo"),
            compact_trigger._trigger_lock_path,
        ),
        (
            "snapshot GC trigger",
            gc_trigger._acquire_worker_lock,
            gc_trigger.release_worker_lock,
            gc_root,
            gc_trigger._worker_lock_path,
        ),
    ]
    for name, acquire, release, target, lock_path in workers:
        fd = acquire(target)
        assert fd is not None, f"{name}: a free lock must be acquirable"
        assert os.path.exists(lock_path(target)), f"{name}: no lock file was created"
        assert acquire(target) is None, f"{name}: a held lock must exclude a second worker"
        release(target, fd)
        assert not os.path.exists(lock_path(target)), f"{name}: release left the lock behind"
        again = acquire(target)
        assert again is not None, f"{name}: a released lock must be re-acquirable"
        release(target, again)


def test_the_six_worker_lock_functions_are_still_module_attributes() -> None:
    """``tests/unit/test_lock_holder_labelling.py:134-135`` monkeypatches
    ``compact_trigger._acquire_trigger_lock`` and ``release_trigger_lock`` BY NAME with no
    ``raising=False``, so removing either attribute makes that test ERROR on attribute-not-found
    rather than fail informatively. All six names are pinned here, where the reason is stated."""
    from rebar._commands import compact_trigger
    from rebar._snapshot import gc_trigger
    from rebar.llm import enrich_drain

    for module, names in (
        (enrich_drain, ("_acquire_advisory_lock", "_release_advisory_lock")),
        (compact_trigger, ("_acquire_trigger_lock", "release_trigger_lock")),
        (gc_trigger, ("_acquire_worker_lock", "release_worker_lock")),
    ):
        for name in names:
            assert callable(getattr(module, name)), f"{module.__name__}.{name} is not callable"
