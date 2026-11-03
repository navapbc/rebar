"""A compaction fold does its identity and signer lookups BEFORE it takes the store write lock,
runs no freshness fetch while it holds the lock, and reports each phase's time.

Bug breezelike-travelsick-moray: replaying the production sweep on a clone of the 300k-commit
tickets store measured the first fold's store-wide identity scan (``resolve_current_identity``
reducing every ticket dir) at 11-57 s under the lock. Ordinary writes resolve the same
attribution before locking. Bug congruent-demurrable-insect: ``identify_signer`` reached
``rebar.show_ticket`` -> ``ensure_fresh``, which fetched and then asked for the store lock the
fold already held (a 2 s self-wait per fold).

Every probe observes STATE (is the store lock dir present at the call?) rather than duration.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import rebar
from rebar._commands import _seam, identity
from rebar._commands import compact as _compact
from rebar._commands import compact_txn as _compact_txn
from rebar.attest import authorship, sshsig

pytestmark = pytest.mark.unit

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001 — best-effort SSHSIG availability probe; skip if unavailable
    _SSH_OK = False
_ssh = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")

_HOUR_NS = 3_600_000_000_000
_EMAIL = "dev@example.com"


def _git(cwd: Path | str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A store whose identity is found by git email (no pointer), so attribution runs the
    store-wide scan. The process-wide attribution cache is cleared on entry and exit, so no
    earlier test's cached identity hides the scan and none leaks out of this one."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", _EMAIL),
        ("config", "user.name", "Dev"),
        ("commit", "-q", "--allow-empty", "-m", "init"),
    ):
        _git(repo, *args)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    rebar.init_repo(repo_root=str(repo))
    _seam._reset_attribution_cache()
    try:
        yield repo
    finally:
        _seam._reset_attribution_cache()


def _tracker(repo: Path) -> str:
    return str(rebar.config.tracker_dir(str(repo)))


def _lock_dir(repo: Path) -> Path:
    return Path(_tracker(repo)) / ".ticket-write.lock.d"


def _age_and_commit(repo: Path, tid: str) -> None:
    """Move every event an hour into the past so the horizon lets the fold take it."""
    tdir = Path(_tracker(repo)) / tid
    for path in sorted(tdir.glob("*.json")):
        if path.name.startswith("."):
            continue
        event = json.loads(path.read_text())
        event["timestamp"] -= _HOUR_NS
        rest = path.name.split("-", 1)[1]
        path.unlink()
        (tdir / f"{event['timestamp']}-{rest}").write_text(json.dumps(event))
    _git(_tracker(repo), "add", "-A")
    _git(_tracker(repo), "commit", "-q", "--no-verify", "-m", "age events")


def _seed(repo: Path, title: str = "t", comments: int = 3) -> str:
    tid = rebar.create_ticket("task", title, description="x" * 60, repo_root=str(repo))
    for i in range(comments):
        rebar.comment(tid, f"c{i}", repo_root=str(repo))
    resolved = rebar._ids.resolve_ticket_id(tid, _tracker(repo))
    assert resolved is not None
    _age_and_commit(repo, resolved)
    return resolved


def _signing_identity(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """An identity matching the git email, with a real SSH key, so events carry decodable
    signatures and the ledger genuinely reaches ``identify_signer``."""
    key = tmp_path / "author"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", "author"],
        check=True,
        capture_output=True,
    )
    parts = (tmp_path / "author.pub").read_text().strip().split()
    ident = rebar.create_identity(
        "Dev", _EMAIL, keys=[f"{parts[0]} {parts[1]}"], repo_root=str(repo)
    )
    monkeypatch.setenv("REBAR_IDENTITY_SIGNING_KEY", str(key))
    # Earlier setup writes cached "no identity" for this repo; drop it so new events sign.
    _seam._reset_attribution_cache()
    return ident


def _signed_events(repo: Path, tid: str) -> list[str]:
    tdir = Path(_tracker(repo)) / tid
    paths = sorted(str(p) for p in tdir.glob("*.json") if not p.name.startswith("."))
    signed = [p for p in paths if json.loads(Path(p).read_text()).get("author_sig")]
    assert signed, "ANCHOR: the fixture must produce signed events, or the probe sees nothing"
    return paths


def _record_lock_state(monkeypatch, repo, module, name, seen: list[tuple[str, bool]]) -> None:
    real = getattr(module, name)
    lock_dir = _lock_dir(repo)

    def wrapper(*a, **k):
        seen.append((name, lock_dir.exists()))
        return real(*a, **k)

    monkeypatch.setattr(module, name, wrapper)


def _expire_freshness_throttle(repo: Path) -> None:
    """Remove the at-most-once-per-minute read-sync marker, so a freshness read inside the
    fold would really fetch — otherwise setup writes leave it fresh and the probe is blind."""
    tracker_abs = os.path.realpath(_tracker(repo))
    marker = f"/tmp/.ticket-sync-{hashlib.md5(tracker_abs.encode()).hexdigest()[:12]}"
    try:
        os.remove(marker)
    except FileNotFoundError:
        pass


# ── identity attribution is resolved before the lock ─────────────────────────────────────
def test_the_store_wide_identity_scan_runs_before_the_lock(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = store
    rebar.create_identity("Dev", _EMAIL, repo_root=str(repo))
    _seed(repo)
    _seam._reset_attribution_cache()
    seen: list[tuple[str, bool]] = []
    _record_lock_state(monkeypatch, repo, identity, "_match_by_email", seen)

    _compact.compact_all_cli([], repo_root=str(repo))

    assert seen, "ANCHOR: the fold must resolve its attribution, or this asserts nothing"
    assert not [s for s in seen if s[1]], (
        f"the store-wide identity scan ran while the fold held the store lock: {seen}"
    )


def test_the_snapshot_still_carries_the_resolved_author_id(store: Path) -> None:
    repo = store
    ident = rebar.create_identity("Dev", _EMAIL, repo_root=str(repo))
    tid = _seed(repo)
    _seam._reset_attribution_cache()

    _compact.compact_all_cli([], repo_root=str(repo))

    snaps = sorted((Path(_tracker(repo)) / tid).glob("*-SNAPSHOT.json"))
    assert snaps, "precondition: the ticket folded"
    snap = json.loads(snaps[-1].read_text())
    assert snap.get("author_id") == ident
    assert snap.get("author_email") == _EMAIL


# ── signer lookups are resolved before the lock ──────────────────────────────────────────
@_ssh
def test_signer_lookups_run_before_the_lock(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = store
    _signing_identity(repo, tmp_path, monkeypatch)
    tid = _seed(repo)
    _signed_events(repo, tid)
    seen: list[tuple[str, bool]] = []
    _record_lock_state(monkeypatch, repo, authorship, "identify_signer", seen)

    _compact.compact_all_cli([], repo_root=str(repo))

    assert seen, "ANCHOR: signed events must reach identify_signer"
    assert not [s for s in seen if s[1]], (
        f"a signer lookup ran while the fold held the store lock: {seen}"
    )


@_ssh
def test_presolved_signers_leave_the_ledger_unchanged(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = store
    _signing_identity(repo, tmp_path, monkeypatch)
    tid = _seed(repo)
    paths = _signed_events(repo, tid)
    tdir = str(Path(_tracker(repo)) / tid)

    without = _compact_txn._build_authorship_ledger(paths, str(repo))
    signers = _compact_txn.preresolve_signers(tdir, str(repo))
    with_map = _compact_txn._build_authorship_ledger(paths, str(repo), signers=signers)

    assert with_map == without
    assert all(entry["signer_pubkey"] for entry in with_map), (
        "ANCHOR: every signed event must identify its signer, or equality is vacuous"
    )


# ── no freshness fetch and no second lock request while the fold holds the lock ─────────
@_ssh
def test_no_fetch_or_nested_lock_request_while_the_fold_holds_the_lock(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar._store import lock as _lock
    from rebar._store import sync as _sync

    repo = store
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _signing_identity(repo, tmp_path, monkeypatch)
    tid = _seed(repo)
    _signed_events(repo, tid)
    _git(_tracker(repo), "push", "-q", "origin", "HEAD:refs/heads/tickets")
    _expire_freshness_throttle(repo)
    seen: list[tuple[str, bool]] = []
    _record_lock_state(monkeypatch, repo, _sync, "reconverge", seen)
    _record_lock_state(monkeypatch, repo, _sync, "_coordinated_fetch", seen)
    _record_lock_state(monkeypatch, repo, _lock, "acquire", seen)

    _compact.compact_all_cli([], repo_root=str(repo))

    acquires = [s for s in seen if s[0] == "acquire"]
    assert acquires, "ANCHOR: the fold must take the store lock"
    under = [s for s in seen if s[1]]
    assert not under, (
        "a freshness fetch or a second store-lock request ran while the fold held the lock "
        f"(it waits on its own lock): {under}"
    )


# ── per-fold phase timing ────────────────────────────────────────────────────────────────
_PHASES = (
    "attribution",
    "signers",
    "lock_wait",
    "recover",
    "parse",
    "reduce",
    "ledger",
    "build_snapshot",
    "write_retire",
    "commit",
    "held",
)


def test_each_fold_logs_one_named_phase_timing_line_at_warning(
    store: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """WARNING is the level every entrypoint's handler shows by default, so the line reaches
    a production log with no extra configuration."""
    repo = store
    tids = {_seed(repo, f"t{n}") for n in range(2)}

    with caplog.at_level(logging.WARNING, logger="rebar._commands.compact_txn"):
        _compact.compact_all_cli([], repo_root=str(repo))

    lines = [r.getMessage() for r in caplog.records if "compaction fold timing" in r.getMessage()]
    assert len(lines) == 2, f"expected one timing line per fold, got {lines}"
    for tid, line in zip(sorted(tids), sorted(lines), strict=True):
        assert tid in line
        for phase in _PHASES:
            assert f" {phase}=" in line, f"phase {phase!r} missing from {line!r}"


def test_a_fold_that_skips_inside_the_lock_still_reports_its_hold(
    store: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = store
    tid = _seed(repo, comments=1)
    tracker = _tracker(repo)

    with caplog.at_level(logging.WARNING, logger="rebar._commands.compact_txn"):
        rc = _compact_txn._compact_locked(tracker, tid, os.path.join(tracker, tid), 99, True)

    assert rc == 0
    lines = [r.getMessage() for r in caplog.records if "compaction fold timing" in r.getMessage()]
    assert len(lines) == 1 and " held=" in lines[0] and " lock_wait=" in lines[0], lines


def test_the_detached_sweep_worker_writes_the_timing_line_to_its_log(store: Path) -> None:
    """The async trigger runs ``run_sweep`` in a detached child whose stderr IS the worker log.
    No entrypoint installs a handler there, so without one every record is dropped (the
    production worker log was 0 bytes after a sweep)."""
    repo = store
    _seed(repo)
    env = {
        **os.environ,
        "REBAR_ROOT": str(repo),
        "REBAR_COMPACT_THRESHOLD": "1",
        "REBAR_LOG_LEVEL": "",
    }
    code = "import sys; from rebar._commands import compact_trigger as t; t.run_sweep(sys.argv[1])"
    proc = subprocess.Popen(
        [sys.executable, "-c", code, _tracker(repo)],
        cwd=str(repo),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _out, err = proc.communicate(timeout=120)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 0, err
    assert "compaction fold timing" in err, f"the worker log received no timing line:\n{err}"
