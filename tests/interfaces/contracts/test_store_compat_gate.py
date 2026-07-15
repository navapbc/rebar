"""Store-compatibility record + fail-closed capability gate (Finding 2 / story 21dd).

A v1.0 binary must read a committed store-level compatibility record
(``.store-compat.json``: ``format_version`` + ``required_capabilities``) before any
mutating or externally-publishing operation and FAIL CLOSED — before any side effect —
on a record it cannot interpret (unknown format version, unknown required capability,
or a corrupt/unreadable record), while keeping reads and diagnostics available.

Four record states (the load-bearing policy):
  1. ABSENT           -> implicit legacy (version 0), compatible, NOT blocked.
  2. PRESENT+compatible-> pass.
  3. PRESENT+incompatible (unknown format_version OR a required capability not in
     KNOWN_CAPABILITIES) -> fail closed.
  4. PRESENT+corrupt   (JSON parse error / truncation / unreadable) -> fail closed.

The gate lives inside the single lock chokepoint (``lock.acquire()``) so every
lock-holding write is covered by one insertion, plus two explicit gates on the two
write paths that do NOT take the lock (``fsck_recover`` raw-git; the reconciler's
``_apply_mutations`` outbound/inbound Jira push).

Tests assert OBSERVABLE behaviour only: CLI exit codes, stderr text, the presence /
byte-content of on-disk event files, and the raised exception type — never internals.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import rebar

COMPAT_FILE = ".store-compat.json"


# ── helpers ───────────────────────────────────────────────────────────────────
def _cli(*args: str, cwd: str, **env: str) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e.update(env)
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=e,
    )


def _tracker(repo: Path) -> Path:
    return repo / ".tickets-tracker"


def _compat_path(repo: Path) -> Path:
    return _tracker(repo) / COMPAT_FILE


def _write_record(repo: Path, obj) -> None:
    _compat_path(repo).write_text(json.dumps(obj) if not isinstance(obj, str) else obj)


def _seed(repo: Path) -> str:
    return rebar.create_ticket(
        "task",
        "Compat gate task",
        description="Body.\n\n## Acceptance Criteria\n- [ ] a",
        repo_root=str(repo),
    )


def _event_files(repo: Path, tid: str) -> set[str]:
    # Only the canonical event/snapshot files (`<ts>-<uuid>-<TYPE>.json`) — NOT the
    # reducer's derived `.cache.json` (a read-side cache, not a ticket mutation). The
    # gate blocks the EVENT write; the read that precedes it may still refresh the cache.
    tdir = _tracker(repo) / tid
    return {p.name for p in tdir.glob("*.json") if not p.name.startswith(".")}


def _load_compat_module():
    from rebar._store import compat

    return compat


# ── module surface (happy path) ───────────────────────────────────────────────
def test_module_exports() -> None:
    compat = _load_compat_module()
    assert isinstance(compat.KNOWN_CAPABILITIES, frozenset)
    assert isinstance(compat.CURRENT_FORMAT_VERSION, int)
    assert compat.CURRENT_FORMAT_VERSION >= 1
    assert issubclass(compat.StoreIncompatibleError, Exception)
    assert callable(compat.check_store_compat)


# ── state 1: ABSENT -> pass-through (happy path) ──────────────────────────────
def test_absent_record_passes(rebar_repo: Path) -> None:
    # A freshly-initialized store may already carry the record (ensure unit); remove
    # it to exercise the ABSENT (implicit-legacy) branch explicitly.
    cp = _compat_path(rebar_repo)
    if cp.exists():
        cp.unlink()
    # A lock-held write must still succeed (absent = legacy, not blocked).
    tid = _seed(rebar_repo)
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "open"


def test_check_store_compat_absent_is_noop(rebar_repo: Path) -> None:
    compat = _load_compat_module()
    cp = _compat_path(rebar_repo)
    if cp.exists():
        cp.unlink()
    # No record -> no exception.
    compat.check_store_compat(str(_tracker(rebar_repo)))


# ── state 2: PRESENT + compatible -> pass (happy path) ────────────────────────
def test_compatible_record_passes(rebar_repo: Path) -> None:
    compat = _load_compat_module()
    _write_record(
        rebar_repo,
        {"format_version": compat.CURRENT_FORMAT_VERSION, "required_capabilities": []},
    )
    tid = _seed(rebar_repo)
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "open"
    compat.check_store_compat(str(_tracker(rebar_repo)))  # no raise


# ── ensure unit stamps the record when absent (happy path) ────────────────────
def test_ensure_unit_writes_record_when_absent(rebar_repo: Path) -> None:
    compat = _load_compat_module()
    # The store was initialized by the fixture; the ensure sweep must have stamped
    # a present+compatible record at the canonical path.
    cp = _compat_path(rebar_repo)
    assert cp.exists(), "ensure unit did not write .store-compat.json"
    rec = json.loads(cp.read_text())
    assert rec["format_version"] == compat.CURRENT_FORMAT_VERSION
    assert rec["required_capabilities"] == []
    # And it is a COMMITTED file on the tickets branch (not gitignored).
    tracker = _tracker(rebar_repo)
    out = subprocess.run(
        ["git", "-C", str(tracker), "ls-files", "--error-unmatch", COMPAT_FILE],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, f".store-compat.json is not tracked/committed: {out.stderr}"


# ══════════════════════════════════════════════════════════════════════════════
#  HELD-OUT ORACLE — edge / fail-closed / E2E (withheld from the implementer)
# ══════════════════════════════════════════════════════════════════════════════


# ── state 3a: unknown format_version -> fail closed, store byte-unchanged ──────
def test_unknown_format_version_blocks_lockheld_write(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)  # seed while still compatible
    before = _event_files(rebar_repo, tid)
    _write_record(rebar_repo, {"format_version": 99, "required_capabilities": []})
    cp = _cli("comment", tid, "should be blocked", cwd=str(rebar_repo))
    assert cp.returncode != 0, f"write was NOT blocked: {cp.stdout}{cp.stderr}"
    assert "99" in (cp.stdout + cp.stderr) or "format" in (cp.stdout + cp.stderr).lower()
    # Store event files byte-unchanged: no new event appended.
    assert _event_files(rebar_repo, tid) == before, "an event was written despite the gate"


# ── state 3b: unknown required capability -> fail closed ──────────────────────
def test_unknown_capability_blocks_lockheld_write(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    before = _event_files(rebar_repo, tid)
    _write_record(rebar_repo, {"format_version": 1, "required_capabilities": ["unknown-v99"]})
    cp = _cli("comment", tid, "blocked by capability", cwd=str(rebar_repo))
    assert cp.returncode != 0
    assert "unknown-v99" in (cp.stdout + cp.stderr)
    assert _event_files(rebar_repo, tid) == before


def test_check_store_compat_raises_on_incompatible(rebar_repo: Path) -> None:
    compat = _load_compat_module()
    _write_record(rebar_repo, {"format_version": 99, "required_capabilities": []})
    with pytest.raises(compat.StoreIncompatibleError):
        compat.check_store_compat(str(_tracker(rebar_repo)))
    _write_record(rebar_repo, {"format_version": 1, "required_capabilities": ["cap-x"]})
    with pytest.raises(compat.StoreIncompatibleError):
        compat.check_store_compat(str(_tracker(rebar_repo)))


# ── state 4: corrupt / unreadable -> fail closed, message names path ──────────
def test_corrupt_record_blocks_and_names_path(rebar_repo: Path) -> None:
    compat = _load_compat_module()
    _write_record(rebar_repo, '{"format_version": 1, "required_capab')  # truncated JSON
    with pytest.raises(compat.StoreIncompatibleError) as ei:
        compat.check_store_compat(str(_tracker(rebar_repo)))
    msg = str(ei.value)
    assert COMPAT_FILE in msg, "diagnostic must name the record path"


def test_corrupt_record_blocks_lockheld_write(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    before = _event_files(rebar_repo, tid)
    _write_record(rebar_repo, "{not json at all")
    cp = _cli("comment", tid, "blocked by corruption", cwd=str(rebar_repo))
    assert cp.returncode != 0
    assert _event_files(rebar_repo, tid) == before


# ── read/diagnostic allowance: reads exit 0; fsck repair (write) blocked ───────
def test_reads_allowed_under_incompatible_record(rebar_repo: Path) -> None:
    tid = _seed(rebar_repo)
    _write_record(rebar_repo, {"format_version": 99, "required_capabilities": []})
    # Reads stay available (exit 0) — they hold no write lock.
    for args in (("show", tid), ("list",), ("search", "task")):
        cp = _cli(*args, cwd=str(rebar_repo))
        assert cp.returncode == 0, f"read command {args} was blocked: {cp.stderr}"
    # fsck diagnostic stays available (exit 0) AND surfaces a structured compat_error.
    cp = _cli("fsck", "--output", "json", cwd=str(rebar_repo))
    assert cp.returncode == 0, f"fsck diagnostic was blocked: {cp.stderr}"
    report = json.loads(cp.stdout)
    assert report.get("compat_error", {}).get("kind") == "unknown_format_version", (
        f"fsck diagnostic must report a structured compat_error: {cp.stdout}"
    )
    assert "99" in report["compat_error"]["detail"]


def test_fsck_repair_blocked_under_incompatible_record(rebar_repo: Path) -> None:
    _seed(rebar_repo)
    _write_record(rebar_repo, {"format_version": 99, "required_capabilities": []})
    # fsck --repair takes the write lock -> gated -> non-zero exit.
    cp = _cli("fsck", "--repair", cwd=str(rebar_repo))
    assert cp.returncode != 0, "fsck --repair (write-intent) must be blocked"


# ── fsck_recover explicit gate (raw git, no lock) ─────────────────────────────
def test_fsck_recover_gated_under_incompatible_record(rebar_repo: Path) -> None:
    _seed(rebar_repo)
    _write_record(rebar_repo, {"format_version": 99, "required_capabilities": []})
    cp = _cli(
        "fsck-recover",
        "--tracker-dir",
        str(_tracker(rebar_repo)),
        cwd=str(rebar_repo),
    )
    assert cp.returncode != 0, "fsck-recover must be gated on an incompatible record"


def test_fsck_recover_detect_only_allowed_under_incompatible(rebar_repo: Path) -> None:
    """`--detect-only` is a read-only diagnostic (no git mutation) — it stays available
    under an incompatible record, mirroring the fsck diagnostic read-allowance."""
    _seed(rebar_repo)
    _write_record(rebar_repo, {"format_version": 99, "required_capabilities": []})
    cp = _cli(
        "fsck-recover",
        "--tracker-dir",
        str(_tracker(rebar_repo)),
        "--detect-only",
        cwd=str(rebar_repo),
    )
    assert cp.returncode == 0, (
        f"fsck-recover --detect-only (read-only) must stay available: {cp.stderr}"
    )


# ── optional-additive event still preserve-and-ignored (NOT fail-closed) ──────
def test_optional_additive_event_not_blocked(rebar_repo: Path) -> None:
    """An unknown *event type* is optional-additive (preserve-and-ignore); it must
    NOT trip the fail-closed gate — only an unknown *required capability* does."""
    compat = _load_compat_module()
    _write_record(
        rebar_repo,
        {"format_version": compat.CURRENT_FORMAT_VERSION, "required_capabilities": []},
    )
    tid = _seed(rebar_repo)
    # Hand-write a future/unknown-type event file.
    tdir = _tracker(rebar_repo) / tid
    env_id = (_tracker(rebar_repo) / ".env-id").read_text().strip()
    ts = 1_781_000_000_000_000_000
    uuid = "ffffffff-0000-4000-8000-000000000042"
    event = {
        "event_type": "FUTURE_TYPE",
        "timestamp": ts,
        "uuid": uuid,
        "env_id": env_id,
        "author": "newer-rebar",
        "data": {"x": 1},
    }
    (tdir / f"{ts}-{uuid}-FUTURE_TYPE.json").write_text(json.dumps(event))
    # Reads succeed (preserve-and-ignore) AND a further lock-held write is allowed,
    # because the compatible record permits writes and the unknown event is optional.
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "open"
    cp = _cli("comment", tid, "still writable", cwd=str(rebar_repo))
    assert cp.returncode == 0, f"optional-additive event wrongly blocked writes: {cp.stderr}"


# ── run_ensures must NOT swallow the gate (fail-closed integrity) ─────────────
def test_run_ensures_fail_closed_under_incompatible(rebar_repo: Path) -> None:
    """run_ensures wraps its write_lock body in a broad `except Exception` (its
    "Never raises" sweep contract). The gate raises StoreIncompatibleError INSIDE
    lock.acquire(); run_ensures must re-raise it (not swallow as a sweep no-op), so
    init / MCP-boot / fsck --repair fail closed on an incompatible store."""
    from rebar._store import compat, ensures

    _write_record(rebar_repo, {"format_version": 99, "required_capabilities": []})
    with pytest.raises(compat.StoreIncompatibleError):
        ensures.run_ensures(str(_tracker(rebar_repo)))


# ── reconcile _apply_mutations gate (outbound Jira push chokepoint) ───────────
def _load_reconcile():
    # `rebar_reconciler` is an engine top-level package (src/rebar/_engine on sys.path),
    # BUT under pytest a test-dir package `tests/unit/rebar_reconciler/__init__.py`
    # shadows it — so a naive `from rebar_reconciler import reconcile` is import-order
    # fragile. Mirror tests/unit/rebar_reconciler/conftest.py: extend whichever
    # `rebar_reconciler` is bound with the engine dir on its __path__ so the engine's
    # `reconcile` submodule resolves regardless of collection order.
    engine_dir = Path(rebar.__file__).resolve().parent / "_engine"
    if str(engine_dir) not in sys.path:
        sys.path.insert(0, str(engine_dir))
    import rebar_reconciler  # may be the test-dir shadow package under pytest

    engine_pkg = str(engine_dir / "rebar_reconciler")
    if engine_pkg not in getattr(rebar_reconciler, "__path__", []):
        rebar_reconciler.__path__.append(engine_pkg)
    from rebar_reconciler import reconcile

    return reconcile


def test_reconcile_apply_mutations_gated(rebar_repo: Path) -> None:
    reconcile = _load_reconcile()
    compat = _load_compat_module()
    _write_record(rebar_repo, {"format_version": 99, "required_capabilities": []})

    class _ExplodingApplier:
        def apply(self, *a, **k):  # pragma: no cover - must never be reached
            raise AssertionError("applier.apply called despite incompatible store")

    ctx = reconcile._PassContext(
        pass_id="t",
        repo_root=rebar_repo,
        persist=True,
        applier=_ExplodingApplier(),
        mutations=[],
    )
    with pytest.raises(compat.StoreIncompatibleError):
        reconcile._apply_mutations(ctx)


def test_reconcile_dryrun_not_gated(rebar_repo: Path) -> None:
    """A non-persisting (dry-run) pass must NOT be gated — no outbound write occurs."""
    reconcile = _load_reconcile()
    _write_record(rebar_repo, {"format_version": 99, "required_capabilities": []})

    calls = {"n": 0}

    class _RecordingApplier:
        def apply(self, *a, **k):
            calls["n"] += 1
            return {}  # nowrite plan dict

    # Minimal context for a dry-run pass. If construction/threading requires more
    # wiring, this asserts the gate does not fire before persist is consulted.
    ctx = reconcile._PassContext(
        pass_id="t",
        repo_root=rebar_repo,
        persist=False,
        applier=_RecordingApplier(),
        mutations=[],
    )
    # Should not raise StoreIncompatibleError for a dry-run; the applier is reached.
    reconcile._apply_mutations(ctx)
    assert calls["n"] == 1


# ── completeness: the gate is at the structural chokepoint (CI lint) ──────────
def test_gate_wired_at_all_write_chokepoints() -> None:
    """Grep the tree: the lock chokepoint gates every acquire, and the two
    lock-bypassing write paths (fsck_recover, reconcile._apply_mutations) each call
    check_store_compat — so a new un-gated write path fails CI rather than relying on
    a hand-maintained prose list."""
    src = Path(rebar.__file__).resolve().parent
    lock_txt = (src / "_store" / "lock.py").read_text()
    # The single acquire() must invoke the gate.
    acq = lock_txt[lock_txt.index("def acquire(") :]
    acq = acq[: acq.index("\ndef ")]
    assert "check_store_compat" in acq, "lock.acquire() does not call check_store_compat"

    recover_txt = (src / "_commands" / "fsck_recover.py").read_text()
    assert "check_store_compat" in recover_txt, "fsck_recover missing explicit gate"

    reconcile_txt = (src / "_engine" / "rebar_reconciler" / "reconcile.py").read_text()
    assert "check_store_compat" in reconcile_txt, "reconcile._apply_mutations missing explicit gate"

    # No OTHER definition of acquire()/write_lock() bypasses the gate: there is exactly
    # one of each, both in lock.py.
    for other in ("txn.py", "compact.py", "fsck.py", "event_append.py", "sync.py", "push.py"):
        txt = (
            (src / "_commands" / other).read_text()
            if (src / "_commands" / other).exists()
            else (src / "_store" / other).read_text()
        )
        assert "def acquire(" not in txt, f"{other} defines a competing acquire()"


# ── AC2: a missed ensure sweep must NOT block writes (defers stamping only) ────
def test_missed_ensure_sweep_does_not_block_writes(rebar_repo: Path, monkeypatch) -> None:
    """If the ensure sweep that would stamp the record is missed (run_ensures raising
    LockTimeout), the record stays ABSENT — and absent = legacy pass-through, so a
    subsequent lock-held write still succeeds. The sweep only DEFERS stamping the record
    to a later covered operation; it never gates writes."""
    from rebar._store import ensures
    from rebar._store.lock import LockTimeout

    # Simulate a record-less store whose stamping sweep fails.
    cp = _compat_path(rebar_repo)
    if cp.exists():
        cp.unlink()

    def _boom(*a, **k):
        raise LockTimeout(30)

    monkeypatch.setattr(ensures, "run_ensures", _boom)
    # A write on the still-absent record must succeed (pass-through), not be blocked.
    tid = _seed(rebar_repo)
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "open"


# ── AC5: rollback safety — a pre-v1.0 binary ignores the committed record ──────
def test_rollback_pre_v1_binary_ignores_record(rebar_repo: Path, monkeypatch) -> None:
    """Emulate a pre-v1.0 binary (which predates the gate) by patching out the
    check_store_compat reference the lock path actually calls, against a store carrying
    a committed .store-compat.json: the store stays operable (a normal write succeeds via
    the library; list/fsck exit 0) and the .store-compat.json is byte-unchanged — i.e.
    the record is an ignored-unknown file for old code, so rollback is safe."""
    compat = _load_compat_module()
    _write_record(
        rebar_repo,
        {"format_version": compat.CURRENT_FORMAT_VERSION, "required_capabilities": []},
    )
    before = _compat_path(rebar_repo).read_bytes()

    # lock.py imported check_store_compat by name at load, so patch that reference.
    from rebar._store import lock as lockmod

    monkeypatch.setattr(lockmod, "check_store_compat", lambda *a, **k: None)

    # Reads stay operable (real subprocess gate is a no-op on a compatible record too).
    assert _cli("list", cwd=str(rebar_repo)).returncode == 0
    assert _cli("fsck", cwd=str(rebar_repo)).returncode == 0
    # A normal in-process write (gate patched out) succeeds.
    tid = _seed(rebar_repo)
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "open"
    # The committed record is byte-unchanged.
    assert _compat_path(rebar_repo).read_bytes() == before
